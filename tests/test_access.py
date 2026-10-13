import pytest

from helpdesk.iam import approval, auth, permissions
from helpdesk.infra import db
from helpdesk.tools import access


# 查申请记录
def access_requests(requester_id):
    with db.transaction() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT system_id, status, current_approver_id FROM access_requests "
                "WHERE requester_id = ? ORDER BY id",
                (requester_id,),
            )
        ]


# 不传工号查自己，过期的权限不算
def test_query_access_defaults_to_self_and_skips_expired(call_tool):
    result = call_tool(access.query_access, "E1009")
    assert result["employee_id"] == "E1009"
    assert [grant["system_id"] for grant in result["grants"]] == [
        "email",
        "gitlab",
        "jira",
        "vpn",
        "wiki",
    ]


# 范围内的员工查得到
@pytest.mark.parametrize(
    "actor_id, employee_id",
    [
        ("E1006", "E1007"),  # 研发经理查研发员工
        ("E1002", "E1020"),  # IT 管理员查任何人
    ],
)
def test_query_access_in_scope(call_tool, actor_id, employee_id):
    result = call_tool(access.query_access, actor_id, employee_id=employee_id)
    assert result["employee_id"] == employee_id


# 范围外和不存在的员工报同一句错
@pytest.mark.parametrize(
    "actor_id, employee_id",
    [
        ("E1006", "E1020"),  # 研发经理查财务员工
        ("E1007", "E1008"),  # 员工查同事
        ("E1002", "E9999"),  # 工号不存在
    ],
)
def test_query_access_out_of_scope(call_tool, actor_id, employee_id):
    with pytest.raises(ValueError, match=f"找不到员工 {employee_id}"):
        call_tool(access.query_access, actor_id, employee_id=employee_id)


# 低敏系统直接开通
def test_request_low_system_is_granted_immediately(call_tool):
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM grants WHERE employee_id = 'E1007' AND system_id = 'vpn'"
        )
    result = call_tool(
        access.request_access, "E1007", system_id="vpn", reason="居家办公"
    )
    assert result["status"] == "approved"
    assert access_requests("E1007") == [
        {"system_id": "vpn", "status": "approved", "current_approver_id": None}
    ]
    granted = call_tool(access.query_access, "E1007")["grants"]
    assert "vpn" in [grant["system_id"] for grant in granted]


# 中敏等直属经理，高敏等经理 + 系统负责人
@pytest.mark.parametrize(
    "requester_id, system_id, expected_approver_ids",
    [
        ("E1007", "crm", ["E1006"]),
        ("E1027", "sap", ["E1026", "E1019"]),
        ("E1009", "prod_servers", ["E1006", "E1005"]),  # 过期的授权不算已有
    ],
)
def test_request_goes_to_approvers(
    call_tool, requester_id, system_id, expected_approver_ids
):
    result = call_tool(
        access.request_access, requester_id, system_id=system_id, reason="工作需要"
    )
    assert result["status"] == "pending"
    assert [approver["id"] for approver in result["approvers"]] == expected_approver_ids
    assert access_requests(requester_id) == [
        {
            "system_id": system_id,
            "status": "pending",
            "current_approver_id": expected_approver_ids[0],
        }
    ]


# 重复申请返回原申请号，不新建
def test_request_twice_returns_same_request(call_tool):
    first = call_tool(access.request_access, "E1027", system_id="sap", reason="查回款")
    second = call_tool(access.request_access, "E1027", system_id="sap", reason="查回款")
    assert second["status"] == "pending"
    assert second["request_id"] == first["request_id"]
    assert len(access_requests("E1027")) == 1


# 已有权限不用申请
def test_request_already_granted(call_tool):
    result = call_tool(access.request_access, "E1007", system_id="gitlab", reason="x")
    assert result["status"] == "already_granted"
    assert access_requests("E1007") == []


# 系统不存在时报错并列出可选的系统
def test_request_unknown_system(call_tool):
    with pytest.raises(ValueError, match="可选：.*gitlab"):
        call_tool(access.request_access, "E1007", system_id="github", reason="x")


# 只看得到自己的申请，审批中的带上现在等谁审
def test_list_access_requests_shows_own_progress(call_tool):
    call_tool(access.request_access, "E1007", system_id="crm", reason="跟进客户")
    call_tool(access.request_access, "E1008", system_id="crm", reason="看客户工单")
    result = call_tool(access.list_access_requests, "E1007")
    assert result["count"] == 1
    request = result["requests"][0]
    assert (request["system_name"], request["status"], request["reason"]) == (
        "CRM",
        "pending",
        "跟进客户",
    )
    assert request["current_approver_name"] == "王海涛"
    assert request["reject_reason"] is None


# 被驳回的带上驳回理由；按状态过滤
def test_list_access_requests_shows_reject_reason(call_tool):
    request_id = call_tool(
        access.request_access, "E1007", system_id="crm", reason="跟进客户"
    )["request_id"]
    approval.decide_approval(
        request_id,
        auth.Context(user_id="E1006", session_version=0),
        "reject",
        "没有业务需要",
    )
    rejected = call_tool(access.list_access_requests, "E1007", status="rejected")
    assert rejected["count"] == 1
    assert rejected["requests"][0]["reject_reason"] == "没有业务需要"
    assert rejected["requests"][0]["current_approver_name"] is None
    assert call_tool(access.list_access_requests, "E1007", status="pending") == {
        "count": 0,
        "requests": [],
    }


# 工具名都在策略表里，名字写错会被默认拒绝
def test_access_tool_names_match_policies():
    for access_tool in access.ACCESS_TOOLS:
        assert access_tool.name in permissions.TOOL_POLICIES
