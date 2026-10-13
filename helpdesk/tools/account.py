from typing import Annotated, Any

from langchain_core.tools import InjectedToolArg, tool

from helpdesk.iam import permissions


# 查员工，不存在和不在数据范围内报同一句错
def find_employee(connection, actor, tool_name, employee_id):
    employee = connection.execute(
        "SELECT id, name, department, locked_until > datetime('now') AS locked "
        "FROM employees WHERE id = ?",
        (employee_id,),
    ).fetchone()
    if employee is None or not permissions.is_in_scope(
        actor, tool_name, employee["id"], employee["department"]
    ):
        raise ValueError(f"找不到员工 {employee_id}，或你无权操作")
    return employee


# 解锁账号
@tool(parse_docstring=True)
def unlock_account(
    employee_id: str,
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
) -> dict:
    """解锁因连续输错密码被锁定的员工账号。

    Args:
        employee_id: 要解锁的员工工号
    """
    employee = find_employee(connection, actor, "unlock_account", employee_id)
    if not employee["locked"]:
        return {
            "employee_id": employee["id"],
            "name": employee["name"],
            "status": "not_locked",
        }
    connection.execute(
        "UPDATE employees SET locked_until = NULL, failed_logins = 0 WHERE id = ?",
        (employee["id"],),
    )
    return {
        "employee_id": employee["id"],
        "name": employee["name"],
        "status": "unlocked",
    }


# 弹出改密码的输入框，工具本身不碰密码，输入框由命令行在回答下面弹出
@tool
def open_password_form(
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
) -> dict:
    """在回答下面给当前用户弹出改密码的输入框，用邮箱验证码设置新密码，记得或忘了当前密码都用它。输入框用完就关，用户每次说想改密码或忘了密码都要重新调用；不要让用户把密码发在对话里，你也看不到密码。"""
    return {"status": "form_shown"}


# 员工账号的工具
ACCOUNT_TOOLS = [unlock_account, open_password_form]
