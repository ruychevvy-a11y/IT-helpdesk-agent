from typing import Annotated, Any, Literal

from langchain_core.tools import InjectedToolArg, tool

from helpdesk.iam import approval, permissions
from helpdesk.tools import account


# 查权限工具
@tool(parse_docstring=True)
def query_access(
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
    employee_id: str | None = None,
) -> dict:
    """查询员工当前有效的系统权限，已过期的不算。

    Args:
        employee_id: 要查的员工工号，不传则查当前用户自己
    """
    employee = account.find_employee(
        connection, actor, "query_access", employee_id or actor["id"]
    )
    # 过滤掉权限过期的
    grants = connection.execute(
        "SELECT systems.id, systems.name, grants.expires_at FROM grants "
        "JOIN systems ON systems.id = grants.system_id "
        "WHERE grants.employee_id = ? "
        "AND (grants.expires_at IS NULL OR grants.expires_at > datetime('now')) "
        "ORDER BY systems.id",
        (employee["id"],),
    )
    return {
        "employee_id": employee["id"],
        "name": employee["name"],
        "grants": [
            {
                "system_id": row["id"],
                "system_name": row["name"],
                "expires_at": row["expires_at"],
            }
            for row in grants
        ],
    }


# 申请权限
@tool(parse_docstring=True)
def request_access(
    system_id: str,
    reason: str,
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
) -> dict:
    """为当前用户申请系统权限。低敏系统直接开通，中敏、高敏系统提交审批。

    Args:
        system_id: 系统 id，如 gitlab、vpn、sap
        reason: 申请理由，概括用户的原话
    """
    system = connection.execute(
        "SELECT id, name FROM systems WHERE id = ?", (system_id,)
    ).fetchone()
    # 判断是否存在此系统
    if system is None:
        system_ids = [row["id"] for row in connection.execute("SELECT id FROM systems")]
        raise ValueError(f"没有系统 {system_id}，可选：{'、'.join(system_ids)}")
    # 已有权限，不用再申请
    grant = connection.execute(
        "SELECT expires_at FROM grants WHERE employee_id = ? AND system_id = ? "
        "AND (expires_at IS NULL OR expires_at > datetime('now'))",
        (actor["id"], system_id),
    ).fetchone()
    if grant is not None:
        return {
            "status": "already_granted",
            "system_name": system["name"],
            "expires_at": grant["expires_at"],
        }
    # 已有待审批申请，不用重复提交
    pending = connection.execute(
        "SELECT id FROM access_requests "
        "WHERE requester_id = ? AND system_id = ? AND status = 'pending'",
        (actor["id"], system_id),
    ).fetchone()
    if pending is not None:
        return {
            "status": "pending",
            "request_id": pending["id"],
            "system_name": system["name"],
        }
    # 中敏、高敏等第一个审批人，低敏直接开通权限
    approval_chain = permissions.approval_chain(connection, actor["id"], system_id)
    if approval_chain:
        status, current_approver_id = "pending", approval_chain[0]
    else:
        approval.grant_access(connection, actor["id"], system_id)
        status, current_approver_id = "approved", None
    request_id = connection.execute(
        "INSERT INTO access_requests "
        "(requester_id, system_id, reason, status, current_approver_id) "
        "VALUES (?, ?, ?, ?, ?) RETURNING id",
        (actor["id"], system_id, reason, status, current_approver_id),
    ).fetchone()["id"]
    # 要审批的，提交后启动审批线程
    if approval_chain:
        connection.after_commit.append(
            lambda: approval.start_approval(
                request_id, actor["id"], system_id, approval_chain
            )
        )
    # 审批人带上名字，否则工号难懂
    approvers = [
        dict(
            connection.execute(
                "SELECT id, name FROM employees WHERE id = ?", (approver_id,)
            ).fetchone()
        )
        for approver_id in approval_chain
    ]
    return {
        "status": status,
        "request_id": request_id,
        "system_name": system["name"],
        "approvers": approvers,
    }


# 列出自己的权限申请和审批进度
@tool(parse_docstring=True)
def list_access_requests(
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
    status: (
        Literal["pending", "approved", "rejected", "provision_failed"] | None
    ) = None,
) -> dict:
    """列出当前用户自己提交的权限申请和审批进度，用户问「我的申请到哪一步了」「为什么被驳回」时用。status 为 pending 是审批中，current_approver_name 是现在等谁审；approved 是已开通；rejected 是被驳回，reject_reason 是驳回理由；provision_failed 是审批通过但开通失败、已转 IT 处理。

    Args:
        status: 只看某个状态的申请，不传则全部
    """
    requests = connection.execute(
        "SELECT access_requests.id AS request_id, systems.name AS system_name, "
        "access_requests.reason, access_requests.status, "
        "approvers.name AS current_approver_name, access_requests.reject_reason, "
        "strftime('%Y-%m-%d %H:%M', access_requests.created_at, 'localtime') "
        "AS created_at, access_requests.requester_id, requesters.department "
        "FROM access_requests "
        "JOIN systems ON systems.id = access_requests.system_id "
        "JOIN employees AS requesters "
        "ON requesters.id = access_requests.requester_id "
        "LEFT JOIN employees AS approvers "
        "ON approvers.id = access_requests.current_approver_id "
        "ORDER BY access_requests.id"
    )
    visible_requests = [
        dict(request)
        for request in requests
        if permissions.is_in_scope(
            actor,
            "list_access_requests",
            request["requester_id"],
            request["department"],
        )
        and (status is None or request["status"] == status)
    ]
    return {"count": len(visible_requests), "requests": visible_requests}


# 弹出待自己审批的申请列表，工具本身不碰申请，列表由命令行在回答下面弹出
@tool
def open_approval_list(
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
) -> dict:
    """在回答下面给当前用户弹出待他审批的申请列表，用户在列表里选序号批准或驳回。只有调用了这个工具列表才会出现，没调用就不要说已弹出；列表在你的回答下面，不在上面。列表用完就关，用户每次说要审批都要重新调用；你看不到列表内容，也不要替用户做审批决定。"""
    return {"status": "list_shown"}


# 权限申请和审批的工具
ACCESS_TOOLS = [query_access, request_access, list_access_requests, open_approval_list]
