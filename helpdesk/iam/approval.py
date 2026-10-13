from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from helpdesk import config
from helpdesk.iam import auth
from helpdesk.infra import db, notify
from helpdesk.tools import ticket


# 审批线程的状态，完整审批链只存在这里
class ApprovalState(TypedDict):
    request_id: int
    requester_id: str
    system_id: str
    approval_chain: list[str]
    level: int


# 审批流程的配置，线程编号"approval-申请号"
def approval_config(request_id):
    return {"configurable": {"thread_id": f"approval-{request_id}"}}


# 开通权限，按系统的有效天数算到期时间，已有这条授权就覆盖成新的时间
def grant_access(connection, employee_id, system_id):
    connection.execute(
        "INSERT INTO grants (employee_id, system_id, expires_at) "
        "SELECT ?, id, datetime('now', '+' || grant_days || ' days') "
        "FROM systems WHERE id = ? "
        "ON CONFLICT (employee_id, system_id) DO UPDATE "
        "SET granted_at = excluded.granted_at, expires_at = excluded.expires_at",
        (employee_id, system_id),
    )


# 审批结果通知申请人，事务提交后再发
def notify_requester(connection, state, result):
    system = connection.execute(
        "SELECT name FROM systems WHERE id = ?", (state["system_id"],)
    ).fetchone()
    connection.after_commit.append(
        lambda: notify.send_email(
            state["requester_id"],
            f"你申请的 {system['name']} 权限{result}（申请 {state['request_id']}）。",
        )
    )


# 构建审批图，由于发出请求和审批之间需要时间，因此另起审批图
def build_approval_graph(checkpointer):

    # 等这一级审批人决定
    def review(state: ApprovalState, runtime: Runtime[auth.Context]):
        approver_id = state["approval_chain"][state["level"]]
        resume = interrupt(
            {"request_id": state["request_id"], "approver_id": approver_id}
        )
        # 检查实际恢复者是不是审批人，若不是这一级审批人，记审计后继续review等正确的审批人
        resumer = auth.load_actor(runtime.context)
        if resumer["id"] != approver_id:
            with db.transaction() as connection:
                db.record_audit(
                    connection,
                    "approval",
                    actor_id=resumer["id"],
                    target=str(state["request_id"]),
                    decision="deny",
                    detail={"resume": resume, "approver_id": approver_id},
                )
            return Command(goto="review")
        decision, comment = resume["decision"], resume["comment"]
        # 驳回，申请改成 rejected 后结束，不开通
        if decision == "reject":
            with db.transaction() as connection:
                updated = connection.execute(
                    "UPDATE access_requests SET status = 'rejected', "
                    "current_approver_id = NULL, reject_reason = ? "
                    "WHERE id = ? AND current_approver_id = ?",
                    (comment or None, state["request_id"], approver_id),
                ).rowcount
                if updated:
                    db.record_audit(
                        connection,
                        "approval",
                        actor_id=approver_id,
                        target=str(state["request_id"]),
                        decision="reject",
                        detail={"comment": comment},
                    )
                    reason = f"，理由：{comment}" if comment else ""
                    notify_requester(
                        connection, state, f"被 {resumer['name']} 驳回{reason}"
                    )
            return Command(goto=END)
        # 批准，推进到下一级审批人，最后一级批完去开通
        if decision == "approve":
            # 计算下一级审批人，若无则为None，无下一个审批人
            next_level = state["level"] + 1
            next_approver_id = (
                state["approval_chain"][next_level]
                if next_level < len(state["approval_chain"])
                else None
            )
            # 更新审批人，更新成功进审计
            with db.transaction() as connection:
                updated = connection.execute(
                    "UPDATE access_requests SET current_approver_id = ? "
                    "WHERE id = ? AND current_approver_id = ?",
                    (next_approver_id, state["request_id"], approver_id),
                ).rowcount
                if updated:
                    db.record_audit(
                        connection,
                        "approval",
                        actor_id=approver_id,
                        target=str(state["request_id"]),
                        decision="approve",
                    )
            # 最后一级审批完去开通，否则回review等下一个审批人
            if next_approver_id is None:
                return Command(goto="provision")
            return Command(goto="review", update={"level": next_level})
        # 其他决定不处理，继续等
        return Command(goto="review")

    # 开通权限，改状态、授权、审计在一个事务里
    def provision(state: ApprovalState):
        try:
            with db.transaction() as connection:
                # 只有 pending 才开通，防止重跑后重复
                updated = connection.execute(
                    "UPDATE access_requests SET status = 'approved' "
                    "WHERE id = ? AND status = 'pending'",
                    (state["request_id"],),
                ).rowcount
                if updated:
                    grant_access(connection, state["requester_id"], state["system_id"])
                    db.record_audit(
                        connection,
                        "provision",
                        target=str(state["request_id"]),
                        decision="success",
                    )
                    notify_requester(connection, state, "已开通")
        # 开通失败，标成开通失败并给二线建单
        except Exception as error:
            with db.transaction() as connection:
                updated = connection.execute(
                    "UPDATE access_requests SET status = 'provision_failed' "
                    "WHERE id = ? AND status = 'pending'",
                    (state["request_id"],),
                ).rowcount
                if updated:
                    system = connection.execute(
                        "SELECT name FROM systems WHERE id = ?", (state["system_id"],)
                    ).fetchone()
                    created = ticket.insert_ticket(
                        connection,
                        state["requester_id"],
                        "service_request",
                        f"权限开通失败：{system['name']}",
                        f"申请 {state['request_id']} 已审批通过，开通时出错：{error}",
                        "low",
                        "medium",
                        assignee_group="L2",
                    )
                    db.record_audit(
                        connection,
                        "provision",
                        target=str(state["request_id"]),
                        decision="failure",
                        detail={"error": str(error), "ticket_id": created["ticket_id"]},
                    )
                    notify_requester(
                        connection,
                        state,
                        f"审批已通过，但开通失败，IT 已建工单 {created['ticket_id']} 处理",
                    )
        return {}

    graph = StateGraph(ApprovalState, context_schema=auth.Context)
    graph.add_node("review", review)
    graph.add_node("provision", provision)
    graph.add_edge(START, "review")
    graph.add_edge("provision", END)
    return graph.compile(checkpointer=checkpointer)


# 启动审批线程
def start_approval(request_id, requester_id, system_id, approval_chain):
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB_PATH)) as checkpointer:
        build_approval_graph(checkpointer).invoke(
            {
                "request_id": request_id,
                "requester_id": requester_id,
                "system_id": system_id,
                "approval_chain": approval_chain,
                "level": 0,
            },
            approval_config(request_id),
        )


# 待这个人审批的申请，按申请号排
def list_pending_approvals(approver_id):
    with db.transaction() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT access_requests.id AS request_id, access_requests.reason, "
                "strftime('%Y-%m-%d %H:%M', access_requests.created_at, 'localtime') "
                "AS created_at, employees.name AS requester_name, employees.department, "
                "systems.name AS system_name, systems.sensitivity FROM access_requests "
                "JOIN employees ON employees.id = access_requests.requester_id "
                "JOIN systems ON systems.id = access_requests.system_id "
                "WHERE access_requests.current_approver_id = ? "
                "AND access_requests.status = 'pending' "
                "ORDER BY access_requests.id",
                (approver_id,),
            )
        ]


# 审批人对申请做决定，恢复审批线程，返回申请的最新状态
def decide_approval(request_id, context, decision, comment=""):
    # 只认批准和驳回，其他值不碰线程
    if decision not in ("approve", "reject"):
        raise ValueError(f"不支持的审批决定 {decision}")
    # 检查申请，当申请不存在，非等候阶段或非当前用户审核则拒绝
    with db.transaction() as connection:
        request = connection.execute(
            "SELECT status, current_approver_id FROM access_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
    if (
        request is None
        or request["status"] != "pending"
        or request["current_approver_id"] != context.user_id
    ):
        raise ValueError(f"申请 {request_id} 不存在，或非你审核")
    # 检查这条审批线程是否存在且停在等待审批的review节点
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB_PATH)) as checkpointer:
        graph = build_approval_graph(checkpointer)
        if graph.get_state(approval_config(request_id)).next != ("review",):
            raise ValueError(f"申请 {request_id} 的审批线程没在等审批，请转人工")
        graph.invoke(
            Command(resume={"decision": decision, "comment": comment}),
            approval_config(request_id),
            context=context,
        )
    # 查询审批后的最新状态
    with db.transaction() as connection:
        return dict(
            connection.execute(
                "SELECT access_requests.status, systems.name AS system_name, "
                "employees.name AS approver_name FROM access_requests "
                "JOIN systems ON systems.id = access_requests.system_id "
                "LEFT JOIN employees "
                "ON employees.id = access_requests.current_approver_id "
                "WHERE access_requests.id = ?",
                (request_id,),
            ).fetchone()
        )
