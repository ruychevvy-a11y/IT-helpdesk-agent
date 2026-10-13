import json
import logging
import secrets
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from pydantic import BaseModel

import chat
from helpdesk import config
from helpdesk.agents import react
from helpdesk.iam import approval, auth, permissions
from helpdesk.infra import db
from helpdesk.infra.llm import create_chat_model
from helpdesk.tools import ALL_TOOLS, access, account

# 前端打包后的目录
FRONTEND_DIST = config.PROJECT_ROOT / "web" / "dist"

# 审计面板最多显示几条
AUDIT_LIMIT = 100

# 模拟邮箱最多显示几封
OUTBOX_LIMIT = 5

# 登录会话，令牌 --> {"employee": 登录时的员工, "thread_id": 对话线程}
sessions = {}


# 启动时建好 Agent，检查点连接跟着服务的生命周期
@asynccontextmanager
async def lifespan(application):
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB_PATH)) as checkpointer:
        application.state.agent = react.build_react_agent(
            create_chat_model(), ALL_TOOLS, chat.SYSTEM_PROMPT, checkpointer
        )
        yield


app = FastAPI(lifespan=lifespan)


class LoginBody(BaseModel):
    employee_id: str
    password: str


class ChatBody(BaseModel):
    message: str


class ConfirmBody(BaseModel):
    confirmed: bool


class ResetCodeBody(BaseModel):
    employee_id: str


class ResetBody(BaseModel):
    employee_id: str
    code: str
    new_password: str


class DecisionBody(BaseModel):
    decision: str
    comment: str = ""


# 会话对应的运行时身份
def session_context(session):
    employee = session["employee"]
    return auth.Context(
        user_id=employee["id"], session_version=employee["session_version"]
    )


# 按令牌找登录会话，每次回库现查用户，密码被重置过就让登录失效
def current_session(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ")
    session = sessions.get(token)
    if session is None:
        raise HTTPException(401, "请先登录")
    try:
        actor = auth.load_actor(session_context(session))
    except auth.SessionExpired as error:
        sessions.pop(token)
        raise HTTPException(401, "你的密码已被重置，请用新密码重新登录") from error
    session["employee"]["department"] = actor["department"]
    return session


# 登录，失败统一回同一句话，锁定单独提示
@app.post("/api/login")
def login(body: LoginBody):
    decision, employee = auth.authenticate(body.employee_id, body.password)
    if decision == "locked":
        raise HTTPException(
            423,
            f"账号已锁定，{auth.LOCKOUT_MINUTES} 分钟后再试，或用邮箱验证码重置密码",
        )
    if decision != "success":
        raise HTTPException(401, "工号或密码错误")
    token = secrets.token_urlsafe(32)
    sessions[token] = {
        "employee": employee,
        "thread_id": chat.new_thread_id(employee["id"]),
    }
    return {"token": token}


# 退出登录
@app.post("/api/logout")
def logout(authorization: str = Header(default="")):
    sessions.pop(authorization.removeprefix("Bearer "), None)
    return {"ok": True}


# 当前用户、待审批数和命令清单
@app.get("/api/me")
def me(session=Depends(current_session)):
    employee = session["employee"]
    return {
        "id": employee["id"],
        "name": employee["name"],
        "role": employee["role"],
        "department": employee["department"],
        "pending_approvals": len(approval.list_pending_approvals(employee["id"])),
        "commands": {
            command: description
            for command, (description, handler) in chat.COMMANDS.items()
        },
    }


# 跑一段对话，把每一步转成事件；停在确认时把要确认的操作发给前端
def turn_events(agent, session, inputs):
    run_config = {
        "configurable": {"thread_id": session["thread_id"]},
        "recursion_limit": chat.RECURSION_LIMIT,
    }
    try:
        for update in agent.stream(
            inputs, run_config, context=session_context(session), stream_mode="updates"
        ):
            if "__interrupt__" in update:
                yield {
                    "type": "confirm",
                    "operations": update["__interrupt__"][0].value,
                }
                return
            for node, output in update.items():
                for message in output["messages"]:
                    yield from message_events(node, message, session)
    except auth.SessionExpired:
        yield {"type": "expired", "message": "你的密码已被重置，请用新密码重新登录。"}
    # 出错时检查点可能停在「发了工具调用、没有结果」，换新线程
    except GraphRecursionError:
        session["thread_id"] = chat.new_thread_id(session["employee"]["id"])
        yield {
            "type": "error",
            "message": "这个问题处理步骤太多，已停止，建议转人工。对话已重新开始。",
        }
    except Exception:  # pylint: disable=broad-exception-caught
        logging.exception("对话出错")
        session["thread_id"] = chat.new_thread_id(session["employee"]["id"])
        yield {
            "type": "error",
            "message": "系统出错，对话已重新开始；问题持续请转人工。",
        }


# 一条消息转成事件，模型发的工具调用、网关回的结果、最终回答、要弹的界面
def message_events(node, message, session):
    if node == "agent" and message.tool_calls:
        for tool_call in message.tool_calls:
            yield {
                "type": "tool_call",
                "name": tool_call["name"],
                "args": tool_call["args"],
            }
    elif node == "agent":
        yield {"type": "answer", "content": message.content.strip()}
    else:
        yield {
            "type": "tool_result",
            "name": message.name,
            "status": message.status,
            "content": message.content,
        }
        if (
            message.status == "success"
            and message.name == account.open_password_form.name
        ):
            yield {"type": "open_ui", "ui": "password_form"}
        if (
            message.status == "success"
            and message.name == access.open_approval_list.name
        ):
            pending = approval.list_pending_approvals(session["employee"]["id"])
            yield {"type": "open_ui", "ui": "approval_list", "pending": len(pending)}


# 事件流转成 SSE 响应
def event_stream(events):
    return StreamingResponse(
        (f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events),
        media_type="text/event-stream",
    )


# 发一句话给 Agent
@app.post("/api/chat")
def send_message(body: ChatBody, session=Depends(current_session)):
    return event_stream(
        turn_events(
            app.state.agent, session, {"messages": [HumanMessage(body.message)]}
        )
    )


# 回答确认，接着跑停下的那一段
@app.post("/api/chat/confirm")
def confirm(body: ConfirmBody, session=Depends(current_session)):
    return event_stream(
        turn_events(app.state.agent, session, Command(resume=body.confirmed))
    )


# 开一段新对话
@app.post("/api/chat/new")
def new_chat(session=Depends(current_session)):
    session["thread_id"] = chat.new_thread_id(session["employee"]["id"])
    return {"ok": True}


# 已登录改密码，先发验证码
@app.post("/api/password/code")
def password_code(session=Depends(current_session)):
    auth.request_password_reset(session["employee"]["id"])
    return {"message": f"验证码已发到登记邮箱，{auth.RESET_CODE_MINUTES} 分钟内有效。"}


# 忘记密码，登录前发验证码，工号存不存在都回同一句话
@app.post("/api/forgot/code")
def forgot_code(body: ResetCodeBody):
    auth.request_password_reset(body.employee_id)
    return {"message": f"验证码已发到登记邮箱，{auth.RESET_CODE_MINUTES} 分钟内有效。"}


# 用验证码设新密码；已登录的给这次登录换上新的会话版本号，当前会话不被踢
@app.post("/api/password/reset")
def password_reset(body: ResetBody, authorization: str = Header(default="")):
    try:
        session_version = auth.reset_password_with_code(
            body.employee_id, body.code, body.new_password
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    session = sessions.get(authorization.removeprefix("Bearer "))
    if session is not None and session["employee"]["id"] == body.employee_id:
        session["employee"]["session_version"] = session_version
    return {"message": "密码已重置，其他地方的登录已全部失效。"}


# 待自己审批的申请
@app.get("/api/approvals")
def pending_approvals(session=Depends(current_session)):
    return [
        {
            **request,
            "sensitivity": permissions.SENSITIVITY_LABELS[request["sensitivity"]],
        }
        for request in approval.list_pending_approvals(session["employee"]["id"])
    ]


# 批准或驳回，审批人用自己的身份恢复审批线程
@app.post("/api/approvals/{request_id}")
def decide(request_id: int, body: DecisionBody, session=Depends(current_session)):
    try:
        return approval.decide_approval(
            request_id, session_context(session), body.decision, body.comment
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except auth.SessionExpired as error:
        raise HTTPException(401, "你的密码已被重置，请用新密码重新登录") from error


# 审计日志，IT 管理员看全部，其他人只看自己的
@app.get("/api/audit")
def audit_log(session=Depends(current_session)):
    employee = session["employee"]
    everyone = employee["role"] == "it_admin"
    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT id, strftime('%Y-%m-%d %H:%M:%S', created_at, 'localtime') "
            "AS created_at, actor_id, thread_id, event, target, decision, detail "
            "FROM audit_log WHERE ? OR actor_id = ? ORDER BY id DESC LIMIT ?",
            (everyone, employee["id"], AUDIT_LIMIT),
        )
        return [
            {
                **dict(row),
                "detail": json.loads(row["detail"]) if row["detail"] else None,
            }
            for row in rows
        ]


# 演示用的模拟邮箱，代表员工自己的企业邮箱，真实部署里没有这个接口
@app.get("/api/outbox/{employee_id}")
def outbox(employee_id: str):
    if not config.OUTBOX_PATH.exists():
        return []
    lines = config.OUTBOX_PATH.read_text(encoding="utf-8").splitlines()
    mails = [
        line.split("\t", 1)[1] for line in lines if line.startswith(f"{employee_id}\t")
    ]
    return mails[-OUTBOX_LIMIT:][::-1]


# 打包好的前端由这里一起提供
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
