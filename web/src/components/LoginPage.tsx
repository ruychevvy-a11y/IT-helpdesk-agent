import { useState, type FormEvent } from "react";
import { api, ApiError, setToken } from "../api";
import { PasswordReset } from "./PasswordReset";
import { Button, Input } from "./ui";

// 演示账号，初始密码见 data/seed.json
const DEMO_ACCOUNTS = [
  { id: "E1007", label: "李娜 · 研发员工" },
  { id: "E1006", label: "王海涛 · 研发经理" },
  { id: "E1003", label: "刘洋 · IT 管理员" },
  { id: "E1027", label: "冯晓雪 · 市场员工" },
];

export function LoginPage({ notice, onLogin }: { notice: string; onLogin: () => void }) {
  const [employeeId, setEmployeeId] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState(notice);
  const [forgot, setForgot] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const { token } = await api.login(employeeId.trim(), password);
      setToken(token);
      onLogin();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "登录失败");
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-100 to-indigo-100 p-4">
      <div className="w-full max-w-sm rounded-2xl bg-white p-8 shadow-xl">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-indigo-600 text-xl font-bold text-white">
            IT
          </div>
          <h1 className="text-lg font-semibold text-slate-900">IT 服务台助手</h1>
          <p className="mt-1 text-xs text-slate-500">身份来自登录，工具按角色可见，敏感操作要确认</p>
        </div>
        {message && <p className="mb-4 rounded-lg bg-emerald-50 p-2 text-sm text-emerald-700">{message}</p>}
        {forgot ? (
          <PasswordReset
            loggedIn={false}
            onDone={(result) => {
              setForgot(false);
              setMessage(`${result}请用新密码登录。`);
            }}
            onCancel={() => setForgot(false)}
          />
        ) : (
          <form onSubmit={submit} className="space-y-3">
            <Input placeholder="工号，如 E1007" value={employeeId} onChange={(event) => setEmployeeId(event.target.value)} />
            <Input
              type="password"
              placeholder="密码"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
            {error && <p className="text-sm text-rose-600">{error}</p>}
            <div className="flex items-center justify-between">
              <button type="button" className="text-sm text-indigo-600 hover:underline" onClick={() => setForgot(true)}>
                忘记密码
              </button>
              <Button type="submit" disabled={!employeeId || !password}>
                登录
              </Button>
            </div>
          </form>
        )}
        <div className="mt-6 border-t border-slate-100 pt-4">
          <p className="mb-2 text-xs text-slate-500">演示账号（初始密码 Helpdesk@2026）</p>
          <div className="grid grid-cols-2 gap-2">
            {DEMO_ACCOUNTS.map((account) => (
              <button
                key={account.id}
                onClick={() => setEmployeeId(account.id)}
                className="rounded-lg bg-slate-50 px-2 py-1.5 text-left text-xs text-slate-600 ring-1 ring-slate-200 hover:bg-indigo-50"
              >
                <span className="font-mono font-medium text-slate-800">{account.id}</span>
                <br />
                {account.label}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
