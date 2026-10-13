import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

import server
from helpdesk.agents import react
from helpdesk.iam import auth
from helpdesk.infra import db
from helpdesk.tools import ALL_TOOLS
from test_react import ScriptedModel, tool_call_message

PASSWORD = "Helpdesk@2026"


# 起服务，测试结束清掉登录会话
@pytest.fixture
def client():
    with TestClient(server.app) as test_client:
        yield test_client
    server.sessions.clear()


# 把服务里的 Agent 换成按剧本出消息的假模型
def use_model(messages):
    server.app.state.agent = react.build_react_agent(
        ScriptedModel(messages=iter(messages)), ALL_TOOLS, "x", InMemorySaver()
    )


# 登录，返回带令牌的请求头
def login(client, employee_id):
    response = client.post(
        "/api/login", json={"employee_id": employee_id, "password": PASSWORD}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


# SSE 响应拆成事件列表
def events(response):
    return [
        json.loads(part[6:])
        for part in response.text.split("\n\n")
        if part.startswith("data: ")
    ]


# 登录失败统一一句话；没登录访问接口 401；登录后能拿到身份和命令
def test_login_and_me(client):
    wrong = client.post("/api/login", json={"employee_id": "E1007", "password": "x"})
    assert wrong.status_code == 401 and wrong.json()["detail"] == "工号或密码错误"
    assert client.get("/api/me").status_code == 401
    me = client.get("/api/me", headers=login(client, "E1007")).json()
    assert me["role"] == "employee" and me["department"] == "研发"
    assert "/approve" in me["commands"]


# 申请中敏系统：先推确认事件，确认后执行并回答；经理随后在审批接口批准
def test_chat_confirm_then_approve(client):
    use_model(
        [
            tool_call_message("request_access", {"system_id": "crm", "reason": "客户"}),
            AIMessage("已提交，等王海涛审批。"),
        ]
    )
    headers = login(client, "E1007")
    first = events(
        client.post("/api/chat", json={"message": "申请 CRM"}, headers=headers)
    )
    assert [event["type"] for event in first] == ["tool_call", "confirm"]
    second = events(
        client.post("/api/chat/confirm", json={"confirmed": True}, headers=headers)
    )
    assert [event["type"] for event in second] == ["tool_result", "answer"]
    assert second[0]["status"] == "success"

    manager = login(client, "E1006")
    pending = client.get("/api/approvals", headers=manager).json()
    assert [request["system_name"] for request in pending] == ["CRM"]
    decided = client.post(
        f"/api/approvals/{pending[0]['request_id']}",
        json={"decision": "approve"},
        headers=manager,
    ).json()
    assert decided["status"] == "approved"


# 模型调了改密码工具，事件里带上要弹的输入框
def test_password_form_opens_ui(client):
    use_model(
        [
            tool_call_message("open_password_form", {}),
            AIMessage("请在下面的输入框里设置。"),
        ]
    )
    result = events(
        client.post(
            "/api/chat", json={"message": "改密码"}, headers=login(client, "E1007")
        )
    )
    assert {"type": "open_ui", "ui": "password_form"} in result


# 员工伪造看不见的解锁调用，网关拒绝的结果也推给前端
def test_forged_call_is_streamed_as_denied(client):
    use_model(
        [
            tool_call_message("unlock_account", {"employee_id": "E1009"}),
            AIMessage("你没有权限。"),
        ]
    )
    result = events(
        client.post(
            "/api/chat", json={"message": "解锁 E1009"}, headers=login(client, "E1007")
        )
    )
    denied = [event for event in result if event["type"] == "tool_result"]
    assert denied[0]["status"] == "error" and "无权使用" in denied[0]["content"]


# 密码在别处被重置，这个登录下一次请求就 401
def test_session_expires_after_reset(client):
    headers = login(client, "E1007")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE employees SET session_version = session_version + 1 "
            "WHERE id = 'E1007'"
        )
    response = client.get("/api/me", headers=headers)
    assert response.status_code == 401 and "重置" in response.json()["detail"]


# 已登录改密码后，当前会话换上新版本号，不被踢
def test_password_reset_keeps_current_session(client, monkeypatch):
    monkeypatch.setattr(auth, "reset_password_with_code", lambda *args: 1)
    headers = login(client, "E1007")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE employees SET session_version = 1 WHERE id = 'E1007'"
        )
    client.post(
        "/api/password/reset",
        json={"employee_id": "E1007", "code": "123456", "new_password": "x" * 10},
        headers=headers,
    )
    assert client.get("/api/me", headers=headers).status_code == 200


# 审计日志：员工只看自己的，IT 管理员看全部
def test_audit_scope(client):
    employee = login(client, "E1007")
    admin = login(client, "E1003")
    employee_rows = client.get("/api/audit", headers=employee).json()
    admin_rows = client.get("/api/audit", headers=admin).json()
    assert {row["actor_id"] for row in employee_rows} == {"E1007"}
    assert {"E1007", "E1003"} <= {row["actor_id"] for row in admin_rows}
