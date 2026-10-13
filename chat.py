import getpass
import logging
import re
import uuid

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from helpdesk import config
from helpdesk.agents import react
from helpdesk.iam import approval, auth, permissions
from helpdesk.infra.llm import create_chat_model
from helpdesk.tools import ALL_TOOLS, access, account, knowledge, ticket

# 图最多走几步
RECURSION_LIMIT = 25


# 用邮箱验证码重置密码，发码 --> 输验证码和新密码；成功返回新的会话版本号，没成功返回 None
def reset_with_email_code(employee_id):
    auth.request_password_reset(employee_id)
    print(f"验证码已发到登记邮箱，{auth.RESET_CODE_MINUTES} 分钟内有效。")
    code = input("验证码：").strip()
    new_password = getpass.getpass("新密码：")
    if getpass.getpass("再输一次：") != new_password:
        print("两次输入不一致。")
        return None
    try:
        session_version = auth.reset_password_with_code(employee_id, code, new_password)
    except ValueError as error:
        print(error)
        return None
    print("密码已重置，其他地方的登录已全部失效。")
    return session_version


# /forgot 命令，登录前忘了密码，用邮箱验证码重置，设完回到登录
def forgot_command():
    employee_id = input("工号：").strip()
    if reset_with_email_code(employee_id) is not None:
        print("请用新密码登录。")


# 登录，直到成功；工号处输入 /forgot 走忘记密码
def login():
    while True:
        employee_id = input("工号（忘记密码输入 /forgot）：").strip()
        if employee_id == "/forgot":
            forgot_command()
            continue
        password = getpass.getpass("密码：")
        decision, employee = auth.authenticate(employee_id, password)
        if decision == "success":
            return employee
        if decision == "locked":
            print(
                f"账号已锁定，{auth.LOCKOUT_MINUTES} 分钟后再试，或输入 /forgot 用邮箱验证码重置密码。"
            )
        else:
            print("工号或密码错误。")


# /password_change 命令，已登录时修改密码，和忘记密码一样走邮箱验证码；成功后给这次登录换上新的会话版本号，当前会话不会被踢
def password_change_command(employee, argument):
    session_version = reset_with_email_code(employee["id"])
    if session_version is not None:
        employee["session_version"] = session_version


# /logout 命令，返回 True 结束这次登录
def logout_command(employee, argument):
    return True


# /help 命令，列出所有命令
def help_command(employee, argument):
    print(COMMAND_LIST)


# /approve 命令，列出待自己审批的申请，选序号批准或驳回
def approve_command(employee, argument):
    # 列出所有等待的审批
    pending = approval.list_pending_approvals(employee["id"])
    if not pending:
        print("当前没有需要你审批的申请。")
        return
    for number, request in enumerate(pending, start=1):
        sensitivity = permissions.SENSITIVITY_LABELS[request["sensitivity"]]
        print(
            f"{number}. {request['requester_name']}（{request['department']}）申请 "
            f"{request['system_name']}（{sensitivity}），理由：{request['reason']}，"
            f"{request['created_at']} 提交"
        )
    # 输入序号选申请
    choice = input("输入序号，直接回车取消：").strip()
    if not choice:
        return
    if not choice.isdigit() or not 1 <= int(choice) <= len(pending):
        print("请输入正确的序号。")
        return
    # 选批准还是驳回，其他输入都算取消
    answer = input("y 批准，n 驳回，回车取消：").strip().lower()
    if answer not in ("y", "n"):
        return
    decision = "approve" if answer == "y" else "reject"
    # 驳回可以填理由，会通知申请人
    comment = input("驳回理由，可直接回车：").strip() if decision == "reject" else ""
    context = auth.Context(
        user_id=employee["id"], session_version=employee["session_version"]
    )
    try:
        # 根据序列号找到真正的申请号
        request = approval.decide_approval(
            pending[int(choice) - 1]["request_id"], context, decision, comment
        )
    except ValueError as error:
        print(error)
        return
    except auth.SessionExpired:
        print("你的密码已被重置，请用新密码重新登录。")
        return True
    match request["status"]:
        case "approved":
            print(f"已批准，{request['system_name']} 权限已开通。")
        case "rejected":
            print("已驳回。")
        case "provision_failed":
            print("已批准，但开通失败，已自动给 IT 二线建单处理。")
        case "pending":
            print(f"已批准，下一级由 {request['approver_name']} 审批。")


# 对话里的命令 (说明, 处理函数)
COMMANDS = {
    "/password_change": ("修改密码", password_change_command),
    "/logout": ("退出登录", logout_command),
    "/help": ("列出所有命令", help_command),
    "/approve": ("审批待你处理的权限申请", approve_command),
}

# 弹出界面的工具 --> 在回答下面执行的命令
TOOL_COMMANDS = {
    account.open_password_form.name: password_change_command,
    access.open_approval_list.name: approve_command,
}

# 命令清单，一行一个
COMMAND_LIST = "\n".join(
    f"{command}  {description}" for command, (description, handler) in COMMANDS.items()
)

# Agent 的系统提示
SYSTEM_PROMPT = f"""你是公司 IT 服务台的助手，帮员工解答 IT 问题、办理账号权限和工单，用中文简洁回答。
回答用纯文本，不用 Markdown：不要 ** 加粗、# 标题和表格，列举用「1.」「2.」或换行。

【查知识库】
- 用户问怎么做、怎么排查，或者公司的 IT 规则和流程（如 Wi-Fi 连不上、更新能不能不装、申请某个系统要几个人批），先调用 {knowledge.search_kb.name}，再根据查到的文章回答，不要凭常识直接答。
- 只用文章里写了的内容，不要补充文章没写的步骤、例子或猜测。查到的文章都没讲用户问的事，就说知识库里没有相关说明，问用户要不要转人工。
- 用到的文章在回答最后一行写「参考：《标题》」，多篇用顿号隔开。
- 用户只是问规则或流程，就只解答，不要替他提交申请或建单；他明确要办，再调用对应工具。

【查账号数据】
- 用户问已有的权限、申请进度、工单状态，直接调用 {access.query_access.name}、{access.list_access_requests.name}、{ticket.list_tickets.name}、{ticket.query_ticket.name} 查，不用查知识库。
- 只根据工具返回的结果回答，不要编造系统、权限、申请号或工单号；工具报错时，用自己的话把原因告诉用户。

【办事】
- 东西坏了，先查知识库给自助步骤；用户试过不行或直接要报修，再建单。信息不够判断影响范围和紧急程度时，先问一句再建单。
- 调用 {'、'.join(sorted(permissions.CONFIRM_TOOLS))} 之前系统会让用户确认，你直接调用工具，不用先口头问一遍。
- 密码不经过你：用户说要改密码或忘了密码，直接调用 {account.open_password_form.name} 弹出输入框，不要先追问，也别让他把密码发在对话里；收不到验证码邮件的转人工。
- 审批不经过你：用户说要审批、看待审批的申请，直接调用 {access.open_approval_list.name} 在回答下面弹出列表，批准还是驳回由用户自己选，你不要替他决定，也不要自己列申请。
- 用户要找人工，调用 {ticket.handoff_to_human.name}，把工单号告诉用户。你办不了的事，先说明原因，再问用户要不要转人工。

【边界】
- 你只能做工具和命令能做的事，不要编造系统里没有的命令、页面、按钮或流程。用户想做的事有对应命令时，告诉他直接输入命令，命令由系统处理，不经过你。可用命令只有这些：
{COMMAND_LIST}
- 跟 IT 无关的请求（写诗、写文案、闲聊等），礼貌说明你只处理 IT 相关的问题，不调用任何工具。"""


# 新的对话线程编号
def new_thread_id(employee_id):
    return f"chat-{employee_id}-{uuid.uuid4().hex[:8]}"


# 网关要执行需要确认的操作前，把这些操作列给用户，输入 y 才执行
def ask_confirmation(operations):
    for operation in operations:
        print(operation)
    return input("确认执行吗？输入 y 确认，其他取消：").strip().lower() == "y"


# 一个登录身份的对话，命令交给对应的处理函数，其余的话交给 Agent
def chat(agent, employee):
    print(f"你好，{employee['name']}。输入 /help 查看命令，Ctrl+C 退出。")
    # 有待自己审批的申请就提醒
    pending = approval.list_pending_approvals(employee["id"])
    if pending:
        print(f"你有 {len(pending)} 条申请待审批，输入 /approve 查看。")
    thread_id = new_thread_id(employee["id"])
    while True:
        # 每轮之间空一行
        text = input(f"\n{employee['name']}> ").strip()
        if not text:
            continue
        # 命令查表交给处理函数，命令后面的部分是参数；返回 True 就回到登录
        command, _, argument = text.partition(" ")
        if command in COMMANDS:
            _, handler = COMMANDS[command]
            if handler(employee, argument.strip()):
                return
            continue
        if text.startswith("/"):
            print("没有这个命令，输入 /help 查看。")
            continue
        run_config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": RECURSION_LIMIT,
        }
        context = auth.Context(
            user_id=employee["id"], session_version=employee["session_version"]
        )
        # 模型接口偶尔要十几秒，先告诉用户在处理，免得以为卡死了
        print("IT助手 正在处理……")
        try:
            result = agent.invoke(
                {"messages": [HumanMessage(text)]}, run_config, context=context
            )
            # 网关停下来等确认，问完用户接着跑；一轮里可能停不止一次
            while "__interrupt__" in result:
                confirmed = ask_confirmation(result["__interrupt__"][0].value)
                print("IT助手 正在处理……")
                result = agent.invoke(
                    Command(resume=confirmed), run_config, context=context
                )
        # 登录之后密码被重置过，回到登录界面
        except auth.SessionExpired:
            print("你的密码已被重置，请用新密码重新登录。")
            return
        # 出错时检查点可能停在「发了工具调用、没有结果」，接着聊接口会报错，所以换新线程
        except GraphRecursionError:
            print("这个问题处理步骤太多，已停止，建议转人工。对话已重新开始。")
            thread_id = new_thread_id(employee["id"])
            continue
        except Exception:
            logging.exception("对话出错")
            print("系统出错，对话已重新开始；问题持续请转人工。")
            thread_id = new_thread_id(employee["id"])
            continue
        # 本轮调了哪些工具、结果如何，演示时输出看 Agent 做了什么
        tool_messages = [
            message
            for message in react.current_turn(result["messages"])
            if isinstance(message, ToolMessage)
        ]
        for message in tool_messages:
            print(f" [{message.name}] {message.status}：{message.content[:60]}\n")
        answer = result["messages"][-1].content.strip()
        # 本轮调用成功的弹界面工具
        ui_tool_names = [
            message.name
            for message in tool_messages
            if message.status == "success" and message.name in TOOL_COMMANDS
        ]
        if ui_tool_names:
            # 要弹界面的回答压成一行
            answer = "".join(line.strip() for line in answer.splitlines())
            # 没有待审批的，提示接在回答后面，不弹列表
            if (
                access.open_approval_list.name in ui_tool_names
                and not approval.list_pending_approvals(employee["id"])
            ):
                answer += "当前没有需要你审批的申请。"
                ui_tool_names.remove(access.open_approval_list.name)
        else:
            # 回答里的空行去掉
            answer = re.sub(r"\n\s*\n", "\n", answer)
        print(f"IT助手> {answer}")
        # 空一行再弹界面，执行对应的命令
        for tool_name in ui_tool_names:
            print()
            if TOOL_COMMANDS[tool_name](employee, ""):
                return


def main():
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB_PATH)) as checkpointer:
        agent = react.build_react_agent(
            create_chat_model(), ALL_TOOLS, SYSTEM_PROMPT, checkpointer
        )
        while True:
            employee = login()
            chat(agent, employee)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。")
