import json

import pytest
from pydantic import ValidationError

from helpdesk import config
from helpdesk.iam import permissions
from helpdesk.infra import db
from helpdesk.tools import ticket


# 矩阵覆盖影响 × 紧急度的九种组合
def test_priority_matrix_covers_all_combinations():
    levels = ["high", "medium", "low"]
    combinations = {(impact, urgency) for impact in levels for urgency in levels}
    assert set(ticket.PRIORITY_MATRIX) == combinations


# 种子里的历史工单都按矩阵定优先级
def test_seed_tickets_follow_priority_matrix():
    seed = json.loads(config.SEED_PATH.read_text(encoding="utf-8"))
    for seed_ticket in seed["tickets"]:
        level = (seed_ticket["impact"], seed_ticket["urgency"])
        assert ticket.PRIORITY_MATRIX[level] == seed_ticket["priority"]


# 建单，优先级由代码算，提单人是当前用户
def test_create_ticket(call_tool):
    result = call_tool(
        ticket.create_ticket,
        "E1007",
        ticket_type="incident",
        title="显示器闪屏",
        description="外接显示器每隔几分钟闪一下",
        impact="low",
        urgency="medium",
    )
    assert result == {
        "ticket_id": 12,
        "priority": "P4",
        "status": "open",
        "assignee_group": "L1",
    }
    with db.transaction() as connection:
        created = connection.execute(
            "SELECT requester_id, priority FROM tickets WHERE id = 12"
        ).fetchone()
    assert dict(created) == {"requester_id": "E1007", "priority": "P4"}


# 影响填了不认识的值，参数校验直接拒绝
def test_create_ticket_rejects_unknown_level(call_tool):
    with pytest.raises(ValidationError):
        call_tool(
            ticket.create_ticket,
            "E1007",
            ticket_type="incident",
            title="x",
            description="x",
            impact="urgent",
            urgency="high",
        )


# 不同身份能看到的工单
@pytest.mark.parametrize(
    "actor_id, expected_ticket_ids",
    [
        ("E1007", [6]),  # 员工只看自己的
        ("E1006", [4, 5, 6, 11]),  # 研发经理看研发部的
        ("E1019", [3, 9]),  # 财务经理看财务部的
        ("E1002", list(range(1, 12))),  # IT 管理员看全部
    ],
)
def test_list_tickets_scope(call_tool, actor_id, expected_ticket_ids):
    result = call_tool(ticket.list_tickets, actor_id)
    ticket_ids = [visible_ticket["ticket_id"] for visible_ticket in result["tickets"]]
    assert ticket_ids == expected_ticket_ids
    assert result["count"] == len(expected_ticket_ids)


# 按状态筛选，研发部只有 11 号是 open
def test_list_tickets_by_status(call_tool):
    result = call_tool(ticket.list_tickets, "E1006", status="open")
    assert [visible_ticket["ticket_id"] for visible_ticket in result["tickets"]] == [11]


# 自己的、本部门的、IT 管理员看任何一张，都查得到
@pytest.mark.parametrize(
    "actor_id, ticket_id",
    [("E1007", 6), ("E1006", 4), ("E1002", 3)],
)
def test_query_ticket_in_scope(call_tool, actor_id, ticket_id):
    result = call_tool(ticket.query_ticket, actor_id, ticket_id=ticket_id)
    assert result["ticket_id"] == ticket_id


# 同事的、别的部门的、不存在的，报同一句错
@pytest.mark.parametrize(
    "actor_id, ticket_id",
    [("E1007", 4), ("E1006", 9), ("E1002", 999)],
)
def test_query_ticket_out_of_scope(call_tool, actor_id, ticket_id):
    with pytest.raises(ValueError, match=f"找不到工单 {ticket_id}"):
        call_tool(ticket.query_ticket, actor_id, ticket_id=ticket_id)


# 工具名都在策略表里，名字写错会被默认拒绝
def test_ticket_tool_names_match_policies():
    for ticket_tool in ticket.TICKET_TOOLS:
        assert ticket_tool.name in permissions.TOOL_POLICIES


# 查单返回本地时间，精确到分钟，和审批列表、查申请一致
def test_query_ticket_returns_local_time(call_tool):
    result = call_tool(ticket.query_ticket, "E1007", ticket_id=6)
    with db.transaction() as connection:
        expected = connection.execute(
            "SELECT strftime('%Y-%m-%d %H:%M', created_at, 'localtime') "
            "FROM tickets WHERE id = 6"
        ).fetchone()[0]
    assert result["created_at"] == expected


# 转人工建一张二线工单，提单人是当前用户，标题带「转人工」，描述是对话摘要
def test_handoff_to_human_creates_l2_ticket(call_tool):
    result = call_tool(
        ticket.handoff_to_human,
        "E1007",
        title="收不到重置密码的验证码",
        summary="用户忘记密码，用 /forgot 重发两次验证码都没收到，企业邮箱也打不开。",
    )
    assert result["assignee_group"] == "L2"
    assert result["priority"] == "P3"
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT requester_id, ticket_type, title, description FROM tickets "
            "WHERE id = ?",
            (result["ticket_id"],),
        ).fetchone()
    assert dict(row) == {
        "requester_id": "E1007",
        "ticket_type": "service_request",
        "title": "转人工：收不到重置密码的验证码",
        "description": "用户忘记密码，用 /forgot 重发两次验证码都没收到，企业邮箱也打不开。",
    }


# 三种角色都能转人工
def test_handoff_to_human_allowed_for_all_roles():
    for role in ("employee", "manager", "it_admin"):
        assert permissions.is_tool_allowed(role, ticket.handoff_to_human.name)
