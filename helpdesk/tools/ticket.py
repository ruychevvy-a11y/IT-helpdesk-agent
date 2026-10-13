from typing import Annotated, Any, Literal

from langchain_core.tools import InjectedToolArg, tool

from helpdesk.iam import permissions

# 优先级矩阵，影响impact × 紧急度urgency --> 优先级priority
PRIORITY_MATRIX = {
    ("high", "high"): "P1",
    ("high", "medium"): "P2",
    ("high", "low"): "P3",
    ("medium", "high"): "P2",
    ("medium", "medium"): "P3",
    ("medium", "low"): "P4",
    ("low", "high"): "P3",
    ("low", "medium"): "P4",
    ("low", "low"): "P4",
}


# 插入一张工单，优先级按影响和紧急度查矩阵
def insert_ticket(
    connection,
    requester_id,
    ticket_type,
    title,
    description,
    impact,
    urgency,
    assignee_group="L1",
):
    priority = PRIORITY_MATRIX[(impact, urgency)]
    ticket = connection.execute(
        "INSERT INTO tickets (requester_id, ticket_type, title, description, "
        "impact, urgency, priority, assignee_group) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "RETURNING id AS ticket_id, priority, status, assignee_group",
        (
            requester_id,
            ticket_type,
            title,
            description,
            impact,
            urgency,
            priority,
            assignee_group,
        ),
    ).fetchone()
    return dict(ticket)


# 建单
@tool(parse_docstring=True)
def create_ticket(
    ticket_type: Literal["incident", "service_request"],
    title: str,
    description: str,
    impact: Literal["high", "medium", "low"],
    urgency: Literal["high", "medium", "low"],
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
) -> dict:
    """为当前用户建一张 IT 工单，优先级由影响和紧急度自动算出。

    Args:
        ticket_type: incident 是东西坏了用不了，service_request 是要东西，如装软件、开账号
        title: 一句话标题
        description: 问题描述，保留用户说的现象、时间和影响了谁
        impact: 影响范围，high 多个部门或全公司、或核心业务中断（如没法发版上线），medium 一个部门或几个人，low 只影响提单人自己
        urgency: 紧急程度，high 工作已经停摆或马上有截止时间，medium 影响工作但能临时凑合，low 不着急
    """
    return insert_ticket(
        connection, actor["id"], ticket_type, title, description, impact, urgency
    )


# 查工单
@tool(parse_docstring=True)
def query_ticket(
    ticket_id: int,
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
) -> dict:
    """查看一张工单的详情和处理进度。

    Args:
        ticket_id: 工单号，纯数字，如 12
    """
    ticket = connection.execute(
        "SELECT tickets.id AS ticket_id, tickets.ticket_type, tickets.title, "
        "tickets.description, tickets.priority, tickets.status, "
        "tickets.assignee_group, tickets.requester_id, "
        "strftime('%Y-%m-%d %H:%M', tickets.created_at, 'localtime') AS created_at, "
        "employees.name AS requester_name, employees.department "
        "FROM tickets JOIN employees ON employees.id = tickets.requester_id "
        "WHERE tickets.id = ?",
        (ticket_id,),
    ).fetchone()
    if ticket is None or not permissions.is_in_scope(
        actor, "query_ticket", ticket["requester_id"], ticket["department"]
    ):
        raise ValueError(f"找不到工单 {ticket_id}，或你无权查看")
    return dict(ticket)


# 列工单
@tool(parse_docstring=True)
def list_tickets(
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
    status: Literal["open", "in_progress", "resolved", "closed"] | None = None,
) -> dict:
    """列出当前用户能看到的工单：员工看自己的，经理看本部门的，IT 管理员看全部。

    Args:
        status: 只看某个状态的工单，不传则全部
    """
    tickets = connection.execute(
        "SELECT tickets.id AS ticket_id, tickets.title, tickets.priority, "
        "tickets.status, tickets.requester_id, employees.name AS requester_name, "
        "employees.department "
        "FROM tickets JOIN employees ON employees.id = tickets.requester_id "
        "ORDER BY tickets.id"
    )
    visible_tickets = [
        dict(ticket)
        for ticket in tickets
        if permissions.is_in_scope(
            actor, "list_tickets", ticket["requester_id"], ticket["department"]
        )
        and (status is None or ticket["status"] == status)
    ]
    return {"count": len(visible_tickets), "tickets": visible_tickets}


# 转人工，给二线建单
@tool(parse_docstring=True)
def handoff_to_human(
    title: str,
    summary: str,
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
) -> dict:
    """转人工：给 IT 二线建一张工单，由人工接手。用户明确要找人工，或者知识库和其他工具都解决不了时才用；东西坏了、要装软件这类先用 create_ticket 建普通工单。

    Args:
        title: 一句话标题，说清要人工处理什么
        summary: 对话摘要，写清用户要解决什么、已经试过什么、为什么转人工
    """
    return insert_ticket(
        connection,
        actor["id"],
        "service_request",
        f"转人工：{title}",
        summary,
        "medium",
        "medium",
        assignee_group="L2",
    )


# 工单的工具
TICKET_TOOLS = [create_ticket, query_ticket, list_tickets, handoff_to_human]
