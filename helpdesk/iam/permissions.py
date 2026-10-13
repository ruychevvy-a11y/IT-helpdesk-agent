# 工具策略表，工具 --> 角色 --> 数据范围，表里没有的一律拒绝
# 数据范围 self 只能动自己的，department 本部门，all 全部
TOOL_POLICIES = {
    "search_kb": {"employee": "self", "manager": "self", "it_admin": "self"},
    "query_access": {"employee": "self", "manager": "department", "it_admin": "all"},
    "request_access": {"employee": "self", "manager": "self", "it_admin": "self"},
    "list_access_requests": {"employee": "self", "manager": "self", "it_admin": "self"},
    "unlock_account": {"it_admin": "all"},
    "open_password_form": {"employee": "self", "manager": "self", "it_admin": "self"},
    "open_approval_list": {"employee": "self", "manager": "self", "it_admin": "self"},
    "create_ticket": {"employee": "self", "manager": "self", "it_admin": "self"},
    "query_ticket": {"employee": "self", "manager": "department", "it_admin": "all"},
    "list_tickets": {"employee": "self", "manager": "department", "it_admin": "all"},
    "handoff_to_human": {"employee": "self", "manager": "self", "it_admin": "self"},
}


# 角色能不能用这个工具
def is_tool_allowed(role, tool_name):
    return role in TOOL_POLICIES.get(tool_name, {})


# 这条数据在不在 actor 的数据范围内
def is_in_scope(actor, tool_name, owner_id, owner_department):
    scope = TOOL_POLICIES[tool_name][actor["role"]]
    if scope == "all":
        return True
    if scope == "department":
        return owner_department == actor["department"]
    return owner_id == actor["id"]


# 敏感级别的中文名
SENSITIVITY_LABELS = {"medium": "中敏", "high": "高敏"}


# 申请权限的确认说明，不用确认时返回 None
def request_access_confirmation(connection, args):
    system = connection.execute(
        "SELECT name, sensitivity FROM systems WHERE id = ?", (args.get("system_id"),)
    ).fetchone()
    # 低敏和不存在的系统不用确认
    if system is None or system["sensitivity"] == "low":
        return None
    return (
        f"{system['name']} 是{SENSITIVITY_LABELS[system['sensitivity']]}系统，需要提交审批。"
        f"申请理由：{args.get('reason')}"
    )


# 解锁账号的确认说明，工号不存在时返回 None
def unlock_account_confirmation(connection, args):
    employee = connection.execute(
        "SELECT id, name FROM employees WHERE id = ?", (args.get("employee_id"),)
    ).fetchone()
    if employee is None:
        return None
    return f"即将解锁 {employee['name']}（{employee['id']}）的账号。"


# 要确认的工具 --> 生成确认说明的函数
CONFIRM_TOOLS = {
    "request_access": request_access_confirmation,
    "unlock_account": unlock_account_confirmation,
}


# 审批链，低敏为空，中敏直属经理，高敏再加系统负责人
def approval_chain(connection, requester_id, system_id):
    # 查申请人的经理和系统的sensitivity和负责人
    requester = connection.execute(
        "SELECT manager_id FROM employees WHERE id = ?", (requester_id,)
    ).fetchone()
    system = connection.execute(
        "SELECT sensitivity, owner_id FROM systems WHERE id = ?", (system_id,)
    ).fetchone()
    if system["sensitivity"] == "low":
        return []
    # 总经理无上级则由负责人审核，高敏才需两级审
    levels = [requester["manager_id"] or system["owner_id"]]
    if system["sensitivity"] == "high":
        levels.append(system["owner_id"])
    chain = []
    for approver_id in levels:
        # 不许自己审自己，需到申请人上级
        if approver_id == requester_id:
            approver_id = requester["manager_id"]
        # 当非空，并不在链里，加进chain；防止同个人连批两次
        if approver_id is not None and approver_id not in chain:
            chain.append(approver_id)
    if not chain:
        raise ValueError(f"{requester_id} 申请 {system_id} 找不到审批人")
    return chain
