from helpdesk.iam import auth, permissions
from helpdesk.tools import account


# 锁定的账号解锁后能登录，没锁的返回 not_locked
def test_unlock_account(call_tool):
    for _ in range(auth.MAX_FAILED_LOGINS):
        auth.authenticate("E1008", "wrong-password")
    result = call_tool(account.unlock_account, "E1002", employee_id="E1008")
    assert result["status"] == "unlocked"
    again = call_tool(account.unlock_account, "E1002", employee_id="E1008")
    assert again["status"] == "not_locked"
    # 失败次数也清零了，再输错一次不会马上又锁
    assert auth.authenticate("E1008", "wrong-password") == ("failure", None)
    assert auth.authenticate("E1008", "Helpdesk@2026")[0] == "success"


# 工具名都在策略表里，名字写错会被默认拒绝
def test_account_tool_names_match_policies():
    for account_tool in account.ACCOUNT_TOOLS:
        assert account_tool.name in permissions.TOOL_POLICIES
