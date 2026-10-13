import { useState } from "react";
import { api, ApiError } from "../api";
import { Button, Input } from "./ui";

// 用邮箱验证码设新密码，忘记密码和已登录改密码共用；密码只发给认证接口，不经过模型
export function PasswordReset({
  employeeId,
  loggedIn,
  onDone,
  onCancel,
}: {
  employeeId?: string;
  loggedIn: boolean;
  onDone: (message: string) => void;
  onCancel: () => void;
}) {
  const [targetId, setTargetId] = useState(employeeId ?? "");
  const [codeSent, setCodeSent] = useState(false);
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [mails, setMails] = useState<string[] | null>(null);

  async function sendCode() {
    setError("");
    try {
      const result = loggedIn ? await api.passwordCode() : await api.forgotCode(targetId);
      setNotice(result.message);
      setCodeSent(true);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "发送失败");
    }
  }

  async function submit() {
    setError("");
    if (password !== again) {
      setError("两次输入不一致");
      return;
    }
    try {
      const result = await api.resetPassword(targetId, code.trim(), password);
      onDone(result.message);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "重置失败");
    }
  }

  return (
    <div className="space-y-3">
      {!loggedIn && (
        <Input
          placeholder="工号"
          value={targetId}
          onChange={(event) => setTargetId(event.target.value.trim())}
          disabled={codeSent}
        />
      )}
      {!codeSent ? (
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel}>
            取消
          </Button>
          <Button onClick={sendCode} disabled={!targetId}>
            发送验证码
          </Button>
        </div>
      ) : (
        <>
          <p className="text-sm text-emerald-700">{notice}</p>
          <Input placeholder="6 位验证码" value={code} onChange={(event) => setCode(event.target.value)} />
          <Input
            type="password"
            placeholder="新密码，至少 10 位"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <Input
            type="password"
            placeholder="再输一次"
            value={again}
            onChange={(event) => setAgain(event.target.value)}
          />
          <div className="flex items-center justify-between">
            <button
              className="text-xs text-indigo-600 hover:underline"
              onClick={async () => setMails(await api.outbox(targetId))}
            >
              打开模拟邮箱（演示用）
            </button>
            <div className="flex gap-2">
              <Button variant="secondary" onClick={onCancel}>
                取消
              </Button>
              <Button onClick={submit} disabled={!code || !password}>
                设置新密码
              </Button>
            </div>
          </div>
          {mails && (
            <div className="rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
              <p className="mb-1 font-medium text-slate-700">{targetId} 的邮箱（模拟，真实部署里没有这个入口）</p>
              {mails.length === 0 ? <p>暂无邮件</p> : mails.map((mail, index) => <p key={index}>· {mail}</p>)}
            </div>
          )}
        </>
      )}
      {error && <p className="text-sm text-rose-600">{error}</p>}
    </div>
  );
}
