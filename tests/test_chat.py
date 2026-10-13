import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

import chat
from helpdesk.agents import react
from helpdesk.iam import approval, permissions
from helpdesk.infra import db
from helpdesk.tools import access, account
from test_auth import INITIAL_PASSWORD, last_reset_code
from test_react import ScriptedModel, tool_call_message

# 登录后拿到的李娜，会话版本和种子库一致；改密码会改这个字典，所以每次传一份副本
LI_NA = {"id": "E1007", "name": "李娜", "role": "employee", "session_version": 0}

# 李娜的直属经理王海涛
WANG_HAITAO = {"id": "E1006", "name": "王海涛", "role": "manager", "session_version": 0}


# 按剧本喂键盘输入（含密码），函数到输入时才取值，喂完抛 EOFError，相当于用户按了 Ctrl+Z
def feed_input(monkeypatch, values):
    remaining = iter(values)

    def fake_input(prompt=""):
        value = next(remaining, None)
        if value is None:
            raise EOFError
        return value() if callable(value) else value

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("getpass.getpass", fake_input)


# 密码错提示重输，账号锁了提示找管理员，输对才返回
def test_login_retries_until_success(monkeypatch, capsys):
    with db.transaction() as connection:
        connection.execute(
            "UPDATE employees SET locked_until = datetime('now', '+15 minutes') "
            "WHERE id = 'E1008'"
        )
    feed_input(
        monkeypatch,
        [
            "E1007",
            "wrong-password",
            "E1008",
            INITIAL_PASSWORD,
            "E1007",
            INITIAL_PASSWORD,
        ],
    )
    assert chat.login()["id"] == "E1007"
    output = capsys.readouterr().out
    assert "工号或密码错误" in output and "账号已锁定" in output


# 忘记密码：凭邮件里的验证码重置后，旧密码登录失败、新密码登录成功
def test_forgot_password_then_login(monkeypatch, capsys):
    feed_input(
        monkeypatch,
        ["/forgot", "E1007", last_reset_code, "NewPassword-1", "NewPassword-1"]
        + ["E1007", INITIAL_PASSWORD, "E1007", "NewPassword-1"],
    )
    assert chat.login()["id"] == "E1007"
    output = capsys.readouterr().out
    assert output.index("密码已重置") < output.index("工号或密码错误")


# 对话出错后换新线程，坏掉的检查点不带进下一句
def test_chat_starts_new_thread_after_error(monkeypatch, capsys):
    model = ScriptedModel(
        messages=iter([tool_call_message("query_access", {}), AIMessage("好的")])
    )
    agent = react.build_react_agent(
        model, account.ACCOUNT_TOOLS + access.ACCESS_TOOLS, "x", InMemorySaver()
    )

    def broken_gateway(*args):
        raise RuntimeError("网关出 bug")

    monkeypatch.setattr(react, "execute_tool_call", broken_gateway)
    feed_input(monkeypatch, ["我有哪些权限", "你好"])
    with pytest.raises(EOFError):
        chat.chat(agent, dict(LI_NA))
    output = capsys.readouterr().out
    assert "系统出错" in output and "IT助手> 好的" in output
    with db.transaction() as connection:
        thread_ids = [
            row[0]
            for row in connection.execute(
                "SELECT thread_id FROM audit_log WHERE event = 'llm_call' "
                "GROUP BY thread_id ORDER BY min(id)"
            )
        ]
    assert len(thread_ids) == 2
    second_turn = agent.get_state({"configurable": {"thread_id": thread_ids[1]}})
    assert [type(message).__name__ for message in second_turn.values["messages"]] == [
        "HumanMessage",
        "AIMessage",
    ]


# 别处重置了密码，这边下一句话就被踢回登录，不再调模型
def test_chat_returns_to_login_after_password_reset(monkeypatch, capsys):
    model = ScriptedModel(messages=iter([AIMessage("不该走到这里")]))
    agent = react.build_react_agent(
        model, account.ACCOUNT_TOOLS + access.ACCESS_TOOLS, "x", InMemorySaver()
    )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE employees SET session_version = 1 WHERE id = 'E1007'"
        )
    feed_input(monkeypatch, ["你好"])
    chat.chat(agent, dict(LI_NA))
    output = capsys.readouterr().out
    assert "你的密码已被重置" in output and "IT助手>" not in output


# 对话里改密码走邮箱验证码，这个会话接着能用，库里版本号已经加一
def test_password_change_keeps_current_session(monkeypatch, capsys):
    model = ScriptedModel(messages=iter([AIMessage("好的")]))
    agent = react.build_react_agent(
        model, account.ACCOUNT_TOOLS + access.ACCESS_TOOLS, "x", InMemorySaver()
    )
    feed_input(
        monkeypatch,
        ["/password_change", last_reset_code, "NewPassword-1", "NewPassword-1", "你好"],
    )
    with pytest.raises(EOFError):
        chat.chat(agent, dict(LI_NA))
    output = capsys.readouterr().out
    assert output.index("密码已重置") < output.index("IT助手> 好的")
    with db.transaction() as connection:
        assert (
            connection.execute(
                "SELECT session_version FROM employees WHERE id = 'E1007'"
            ).fetchone()[0]
            == 1
        )


# 命令查表处理：/help 列出所有命令，不存在的命令提示，/logout 回到登录；命令和要确认的工具都在系统提示里
def test_commands_are_dispatched(monkeypatch, capsys):
    agent = react.build_react_agent(
        ScriptedModel(messages=iter([])),
        account.ACCOUNT_TOOLS + access.ACCESS_TOOLS,
        "x",
        InMemorySaver(),
    )
    feed_input(monkeypatch, ["/help", "/nope", "/logout"])
    chat.chat(agent, dict(LI_NA))
    output = capsys.readouterr().out
    for command in chat.COMMANDS:
        assert command in output and command in chat.SYSTEM_PROMPT
    for tool_name in permissions.CONFIRM_TOOLS:
        assert tool_name in chat.SYSTEM_PROMPT
    assert "没有这个命令" in output


# 对话线程编号是 chat-工号-8 位随机串，和审批线程 approval-申请号 区分开
def test_new_thread_id_format():
    thread_id = chat.new_thread_id("E1006")
    assert (
        thread_id.startswith("chat-E1006-") and len(thread_id) == len("chat-E1006-") + 8
    )
    assert chat.new_thread_id("E1006") != thread_id


# 登录时提醒有几条待审批；/approve 列出序号，选号批准，序号不对或回车不处理；批完再看就没有了
def test_approve_command(monkeypatch, capsys, call_tool):
    call_tool(access.request_access, "E1007", system_id="crm", reason="跟进客户")
    agent = react.build_react_agent(
        ScriptedModel(messages=iter([])),
        account.ACCOUNT_TOOLS + access.ACCESS_TOOLS,
        "x",
        InMemorySaver(),
    )
    feed_input(
        monkeypatch,
        ["/approve", "5", "/approve", "", "/approve", "1", "y", "/approve"],
    )
    with pytest.raises(EOFError):
        chat.chat(agent, dict(WANG_HAITAO))
    output = capsys.readouterr().out
    assert "你有 1 条申请待审批，输入 /approve 查看。" in output
    assert "1. 李娜（研发）申请 CRM（中敏），理由：跟进客户，" in output
    assert "请输入正确的序号。" in output
    assert output.count("已批准，CRM 权限已开通。") == 1
    assert output.rstrip().endswith("当前没有需要你审批的申请。")


# 没有待审批的申请，登录不提醒
def test_no_pending_approvals_no_reminder(monkeypatch, capsys):
    agent = react.build_react_agent(
        ScriptedModel(messages=iter([])),
        account.ACCOUNT_TOOLS + access.ACCESS_TOOLS,
        "x",
        InMemorySaver(),
    )
    feed_input(monkeypatch, ["/approve"])
    with pytest.raises(EOFError):
        chat.chat(agent, dict(WANG_HAITAO))
    output = capsys.readouterr().out
    assert "待审批" not in output
    assert "当前没有需要你审批的申请。" in output


# 敏感操作先问用户，输入 y 才执行，其他取消
@pytest.mark.parametrize("answer, request_count", [("y", 1), ("n", 0)])
def test_chat_asks_before_sensitive_tool(monkeypatch, capsys, answer, request_count):
    model = ScriptedModel(
        messages=iter(
            [
                tool_call_message(
                    "request_access", {"system_id": "crm", "reason": "跟进客户"}
                ),
                AIMessage("好的"),
            ]
        )
    )
    agent = react.build_react_agent(
        model, account.ACCOUNT_TOOLS + access.ACCESS_TOOLS, "x", InMemorySaver()
    )
    feed_input(monkeypatch, ["帮我申请 CRM", answer])
    with pytest.raises(EOFError):
        chat.chat(agent, dict(LI_NA))
    assert "CRM 是中敏系统，需要提交审批" in capsys.readouterr().out
    with db.transaction() as connection:
        assert (
            connection.execute("SELECT count(*) FROM access_requests").fetchone()[0]
            == request_count
        )


# 模型调了弹输入框的工具，回答下面就弹出改密码的输入框；密码只在输入框里输，模型的消息里没有
def test_agent_opens_password_change_form(monkeypatch, capsys):
    model = ScriptedModel(
        messages=iter(
            [
                tool_call_message("open_password_form", {}),
                AIMessage("请在下面的输入框里修改密码"),
            ]
        )
    )
    checkpointer = InMemorySaver()
    agent = react.build_react_agent(
        model, account.ACCOUNT_TOOLS + access.ACCESS_TOOLS, "x", checkpointer
    )
    feed_input(
        monkeypatch,
        ["我想改密码", last_reset_code, "NewPassword-1", "NewPassword-1"],
    )
    with pytest.raises(EOFError):
        chat.chat(agent, dict(LI_NA))
    output = capsys.readouterr().out
    assert output.index("IT助手> 请在下面") < output.index("密码已重置")
    with db.transaction() as connection:
        assert (
            connection.execute(
                "SELECT session_version FROM employees WHERE id = 'E1007'"
            ).fetchone()[0]
            == 1
        )
    saved_messages = [
        message.content
        for checkpoint in checkpointer.list(None)
        for message in checkpoint.checkpoint["channel_values"].get("messages", [])
    ]
    assert saved_messages and not any(
        "NewPassword-1" in content for content in saved_messages
    )


# 审批人的密码在别处被重置过，/approve 时提示重新登录并回到登录，不批、不崩
def test_approve_command_with_expired_session(monkeypatch, capsys, call_tool):
    call_tool(access.request_access, "E1007", system_id="crm", reason="跟进客户")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE employees SET session_version = 1 WHERE id = 'E1006'"
        )
    agent = react.build_react_agent(
        ScriptedModel(messages=iter([])),
        account.ACCOUNT_TOOLS + access.ACCESS_TOOLS,
        "x",
        InMemorySaver(),
    )
    feed_input(monkeypatch, ["/approve", "1", "y"])
    chat.chat(agent, dict(WANG_HAITAO))
    assert "你的密码已被重置，请用新密码重新登录。" in capsys.readouterr().out
    with db.transaction() as connection:
        status = connection.execute("SELECT status FROM access_requests").fetchone()
    assert status[0] == "pending"


# 选了序号后回车取消不处理，输入 n 驳回，驳回后列表就空了
def test_approve_command_reject_and_cancel(monkeypatch, capsys, call_tool):
    call_tool(access.request_access, "E1007", system_id="crm", reason="跟进客户")
    agent = react.build_react_agent(
        ScriptedModel(messages=iter([])),
        account.ACCOUNT_TOOLS + access.ACCESS_TOOLS,
        "x",
        InMemorySaver(),
    )
    feed_input(
        monkeypatch,
        ["/approve", "1", "", "/approve", "1", "n", "没有业务需要", "/approve"],
    )
    with pytest.raises(EOFError):
        chat.chat(agent, dict(WANG_HAITAO))
    output = capsys.readouterr().out
    assert output.count("1. 李娜（研发）申请 CRM（中敏）") == 2
    assert output.count("已驳回。") == 1
    assert output.rstrip().endswith("当前没有需要你审批的申请。")
    with db.transaction() as connection:
        status = connection.execute("SELECT status FROM access_requests").fetchone()
    assert status[0] == "rejected"


# 跟 IT 助手说要审批，模型调弹列表的工具，回答下面弹出审批列表，选号批准
def test_agent_opens_approval_list(monkeypatch, capsys, call_tool):
    call_tool(access.request_access, "E1007", system_id="crm", reason="跟进客户")
    model = ScriptedModel(
        messages=iter(
            [
                tool_call_message("open_approval_list", {}),
                AIMessage("待你审批的申请在下面。\n\n选序号就能处理。"),
            ]
        )
    )
    agent = react.build_react_agent(
        model, account.ACCOUNT_TOOLS + access.ACCESS_TOOLS, "x", InMemorySaver()
    )
    feed_input(monkeypatch, ["我要审批", "1", "y"])
    with pytest.raises(EOFError):
        chat.chat(agent, dict(WANG_HAITAO))
    output = capsys.readouterr().out
    assert "IT助手> 待你审批的申请在下面。选序号就能处理。\n\n1. 李娜" in output
    assert "已批准，CRM 权限已开通。" in output


# 批准后开通失败，提示已给二线建单
def test_approve_command_provision_failure(monkeypatch, capsys, call_tool):
    call_tool(access.request_access, "E1007", system_id="crm", reason="跟进客户")

    def broken_grant_access(connection, employee_id, system_id):
        raise RuntimeError("CRM 接口超时")

    monkeypatch.setattr(approval, "grant_access", broken_grant_access)
    agent = react.build_react_agent(
        ScriptedModel(messages=iter([])),
        account.ACCOUNT_TOOLS + access.ACCESS_TOOLS,
        "x",
        InMemorySaver(),
    )
    feed_input(monkeypatch, ["/approve", "1", "y"])
    with pytest.raises(EOFError):
        chat.chat(agent, dict(WANG_HAITAO))
    assert "已批准，但开通失败，已自动给 IT 二线建单处理。" in capsys.readouterr().out


# 说要审批但没有待审批的，回答压成一行，提示接在回答后面，不弹列表
def test_agent_opens_empty_approval_list(monkeypatch, capsys):
    model = ScriptedModel(
        messages=iter(
            [
                tool_call_message("open_approval_list", {}),
                AIMessage(
                    "已为你弹出待审批的申请列表。\n\n也可以直接输入命令 /approve。"
                ),
            ]
        )
    )
    agent = react.build_react_agent(
        model, account.ACCOUNT_TOOLS + access.ACCESS_TOOLS, "x", InMemorySaver()
    )
    feed_input(monkeypatch, ["我要审批"])
    with pytest.raises(EOFError):
        chat.chat(agent, dict(WANG_HAITAO))
    output = capsys.readouterr().out
    assert (
        "IT助手> 已为你弹出待审批的申请列表。也可以直接输入命令 /approve。"
        "当前没有需要你审批的申请。\n" in output
    )
    assert "输入序号" not in output
