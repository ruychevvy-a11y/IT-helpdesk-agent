import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, ROLE_LABELS, sendConfirmation, sendMessage, setToken, type ChatEvent, type Me } from "./api";
import { toolLabel } from "./labels";
import { LoginPage } from "./components/LoginPage";
import { ApprovalPanel, AuditPanel, TracePanel, type Turn } from "./components/Panels";
import { PasswordReset } from "./components/PasswordReset";
import { Badge, Button, Modal, RichText } from "./components/ui";

interface Message {
  id: number;
  role: "user" | "assistant" | "system";
  content: string;
  tools: string[];
}

type Tab = "trace" | "approvals" | "audit";

let nextId = 1;

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [loginNotice, setLoginNotice] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<string[] | null>(null);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("trace");
  const [refreshKey, setRefreshKey] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  async function loadMe() {
    const profile = await api.me();
    setMe(profile);
    setMessages([
      {
        id: nextId++,
        role: "system",
        content:
          `你好，${profile.name}。可以问 IT 问题、申请权限、报修，输入 /help 查看命令。` +
          (profile.pending_approvals ? `你有 ${profile.pending_approvals} 条申请待审批。` : ""),
        tools: [],
      },
    ]);
  }

  function logout(notice = "") {
    api.logout().catch(() => undefined);
    setToken("");
    setMe(null);
    setTurns([]);
    setLoginNotice(notice);
  }

  function addMessage(role: Message["role"], content: string) {
    setMessages((current) => [...current, { id: nextId++, role, content, tools: [] }]);
  }

  // 处理一条对话事件：调用链、确认弹窗、回答、要弹的界面
  function handleEvent(event: ChatEvent) {
    switch (event.type) {
      case "tool_call":
        setTurns((current) =>
          current.map((turn, index) =>
            index === current.length - 1
              ? { ...turn, steps: [...turn.steps, { kind: "call", name: event.name, detail: JSON.stringify(event.args, null, 1) }] }
              : turn,
          ),
        );
        break;
      case "tool_result":
        setTurns((current) =>
          current.map((turn, index) =>
            index === current.length - 1
              ? { ...turn, steps: [...turn.steps, { kind: "result", name: event.name, detail: event.content, status: event.status }] }
              : turn,
          ),
        );
        setMessages((current) => {
          const last = current[current.length - 1];
          if (last?.role !== "assistant" || last.content) {
            return [...current, { id: nextId++, role: "assistant", content: "", tools: [event.name] }];
          }
          return [...current.slice(0, -1), { ...last, tools: [...last.tools, event.name] }];
        });
        break;
      case "answer":
        setMessages((current) => {
          const last = current[current.length - 1];
          if (last?.role === "assistant" && !last.content) {
            return [...current.slice(0, -1), { ...last, content: event.content }];
          }
          return [...current, { id: nextId++, role: "assistant", content: event.content, tools: [] }];
        });
        break;
      case "confirm":
        setConfirmation(event.operations);
        break;
      case "open_ui":
        if (event.ui === "password_form") setPasswordOpen(true);
        else if (event.pending === 0) addMessage("system", "当前没有需要你审批的申请。");
        else setTab("approvals");
        break;
      case "error":
        addMessage("system", event.message);
        break;
      case "expired":
        logout(event.message);
        break;
    }
  }

  // 跑一段对话流，结束后刷新审批和审计
  async function runStream(start: (onEvent: (event: ChatEvent) => void) => Promise<void>) {
    setBusy(true);
    try {
      await start(handleEvent);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) logout(caught.message);
      else addMessage("system", "连接后端失败，请确认 python server.py 正在运行。");
    }
    setBusy(false);
    setRefreshKey((key) => key + 1);
  }

  // 命令在前端处理，和命令行一致，不经过模型
  function runCommand(text: string) {
    const command = text.split(" ")[0];
    if (command === "/help" && me) {
      addMessage("system", Object.entries(me.commands).map(([name, description]) => `${name}  ${description}`).join("\n"));
    } else if (command === "/password_change") {
      setPasswordOpen(true);
    } else if (command === "/approve") {
      setTab("approvals");
    } else if (command === "/logout") {
      logout();
    } else {
      addMessage("system", "没有这个命令，输入 /help 查看。");
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    if (text.startsWith("/")) {
      runCommand(text);
      return;
    }
    addMessage("user", text);
    setTurns((current) => [...current, { id: nextId++, question: text, steps: [] }]);
    await runStream((onEvent) => sendMessage(text, onEvent));
  }

  async function answerConfirmation(confirmed: boolean) {
    setConfirmation(null);
    await runStream((onEvent) => sendConfirmation(confirmed, onEvent));
  }

  async function newChat() {
    await api.newChat();
    setTurns([]);
    if (me) {
      setMessages([{ id: nextId++, role: "system", content: "已开始新对话。", tools: [] }]);
    }
  }

  if (!me) {
    return <LoginPage notice={loginNotice} onLogin={() => loadMe().catch(() => logout("登录已失效"))} />;
  }

  const commandHints = input.startsWith("/")
    ? Object.entries(me.commands).filter(([name]) => name.startsWith(input.split(" ")[0]))
    : [];

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-5 py-3">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-600 text-sm font-bold text-white">IT</div>
          <span className="font-semibold text-slate-900">IT 服务台助手</span>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-sm text-slate-700">
            {me.name} <span className="font-mono text-slate-400">{me.id}</span> · {me.department}
          </span>
          <Badge tone={me.role === "it_admin" ? "indigo" : me.role === "manager" ? "amber" : "slate"}>{ROLE_LABELS[me.role]}</Badge>
          <Button variant="secondary" onClick={newChat}>
            新对话
          </Button>
          <Button variant="secondary" onClick={() => setPasswordOpen(true)}>
            改密码
          </Button>
          <Button variant="secondary" onClick={() => logout()}>
            退出
          </Button>
        </div>
      </header>

      <main className="flex min-h-0 flex-1">
        <section className="flex min-w-0 flex-1 flex-col">
          <div ref={listRef} className="flex-1 space-y-4 overflow-y-auto p-6">
            {messages.map((message) => (
              <div key={message.id} className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}>
                {message.role === "system" ? (
                  <p className="mx-auto max-w-xl rounded-lg bg-slate-200/70 px-3 py-1.5 text-center text-xs whitespace-pre-wrap text-slate-600">
                    {message.content}
                  </p>
                ) : (
                  <div
                    className={`max-w-2xl rounded-2xl px-4 py-2.5 text-sm leading-relaxed shadow-sm ${
                      message.role === "user" ? "bg-indigo-600 text-white" : "bg-white text-slate-800 ring-1 ring-slate-200"
                    }`}
                  >
                    {message.tools.length > 0 && (
                      <div className="mb-1.5 flex flex-wrap gap-1">
                        {message.tools.map((name, index) => (
                          <Badge key={index} tone="indigo">
                            {toolLabel(name)}
                          </Badge>
                        ))}
                      </div>
                    )}
                    {message.content ? <RichText text={message.content} /> : <span className="text-slate-400">处理中……</span>}
                  </div>
                )}
              </div>
            ))}
            {busy && <p className="text-xs text-slate-400">IT 助手正在处理……</p>}
          </div>
          <form onSubmit={submit} className="relative border-t border-slate-200 bg-white p-4">
            {commandHints.length > 0 && (
              <div className="absolute bottom-full left-4 mb-2 rounded-lg bg-white p-2 shadow-lg ring-1 ring-slate-200">
                {commandHints.map(([name, description]) => (
                  <button
                    key={name}
                    type="button"
                    onClick={() => setInput(name)}
                    className="block w-full rounded px-2 py-1 text-left text-sm hover:bg-slate-100"
                  >
                    <span className="font-mono text-indigo-600">{name}</span> <span className="text-slate-500">{description}</span>
                  </button>
                ))}
              </div>
            )}
            <div className="flex gap-2">
              <input
                value={input}
                onChange={(event) => setInput(event.target.value)}
                placeholder="比如：VPN 连不上怎么办 / 我要申请 CRM / 我的申请到哪一步了"
                className="flex-1 rounded-xl border-0 px-4 py-2.5 text-sm ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600 focus:outline-none"
              />
              <Button type="submit" disabled={busy || !input.trim()}>
                发送
              </Button>
            </div>
          </form>
        </section>

        <aside className="flex w-[420px] flex-col border-l border-slate-200 bg-slate-50">
          <nav className="flex border-b border-slate-200 bg-white">
            {(
              [
                ["trace", "调用链"],
                ["approvals", "审批"],
                ["audit", "审计日志"],
              ] as [Tab, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={`flex-1 py-2.5 text-sm font-medium ${
                  tab === key ? "border-b-2 border-indigo-600 text-indigo-600" : "text-slate-500 hover:text-slate-700"
                }`}
              >
                {label}
              </button>
            ))}
          </nav>
          <div className="flex-1 overflow-y-auto">
            {tab === "trace" && <TracePanel turns={turns} />}
            {tab === "approvals" && <ApprovalPanel refreshKey={refreshKey} onDecided={() => setRefreshKey((key) => key + 1)} />}
            {tab === "audit" && <AuditPanel refreshKey={refreshKey} everyone={me.role === "it_admin"} />}
          </div>
        </aside>
      </main>

      {confirmation && (
        <Modal title="确认执行以下操作">
          <ul className="mb-4 space-y-2">
            {confirmation.map((operation, index) => (
              <li key={index} className="rounded-lg bg-amber-50 p-3 text-sm text-amber-800 ring-1 ring-amber-200">
                {operation}
              </li>
            ))}
          </ul>
          <p className="mb-4 text-xs text-slate-500">确认由系统发起，模型不能替你点。</p>
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => answerConfirmation(false)}>
              取消
            </Button>
            <Button onClick={() => answerConfirmation(true)}>确认执行</Button>
          </div>
        </Modal>
      )}

      {passwordOpen && (
        <Modal title="修改密码">
          <p className="mb-3 text-xs text-slate-500">密码只发给认证接口，不经过模型，也不会写进对话记录。</p>
          <PasswordReset
            employeeId={me.id}
            loggedIn
            onDone={(message) => {
              setPasswordOpen(false);
              addMessage("system", message);
            }}
            onCancel={() => setPasswordOpen(false)}
          />
        </Modal>
      )}
    </div>
  );
}
