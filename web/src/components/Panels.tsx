import { useEffect, useState } from "react";
import { api, ApiError, type AuditRow, type PendingApproval } from "../api";
import { DECISION_TONES, EVENT_LABELS, resultKind, toolLabel } from "../labels";
import { Badge, Button, Input } from "./ui";

export interface Step {
  kind: "call" | "result";
  name: string;
  detail: string;
  status?: "success" | "error";
}

export interface Turn {
  id: number;
  question: string;
  steps: Step[];
}

// 调用链：每轮里模型发了什么调用、网关怎么处理的
export function TracePanel({ turns }: { turns: Turn[] }) {
  if (turns.length === 0) {
    return <p className="p-4 text-sm text-slate-500">发一句话后，这里会实时显示模型调了哪些工具、网关放行还是拦下。</p>;
  }
  return (
    <div className="space-y-4 p-4">
      {[...turns].reverse().map((turn) => (
        <div key={turn.id}>
          <p className="mb-2 truncate text-xs font-medium text-slate-500">「{turn.question}」</p>
          {turn.steps.length === 0 ? (
            <p className="text-xs text-slate-400">没有调用工具</p>
          ) : (
            <ol className="relative space-y-2 border-l border-slate-200 pl-4">
              {turn.steps.map((step, index) => {
                const kind = step.kind === "result" ? resultKind(step.status ?? "error", step.detail) : null;
                return (
                  <li key={index} className="relative">
                    <span
                      className={`absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full ${
                        step.kind === "call" ? "bg-indigo-500" : kind?.tone === "green" ? "bg-emerald-500" : "bg-rose-500"
                      }`}
                    />
                    <div className="flex items-center gap-2 text-sm">
                      <span className="font-medium text-slate-800">
                        {step.kind === "call" ? "模型调用" : "网关结果"} · {toolLabel(step.name)}
                      </span>
                      {kind ? <Badge tone={kind.tone}>{kind.label}</Badge> : <Badge tone="indigo">{step.name}</Badge>}
                    </div>
                    <pre className="mt-1 max-h-32 overflow-auto rounded-md bg-slate-50 p-2 text-xs whitespace-pre-wrap text-slate-600">
                      {step.detail.length > 600 ? `${step.detail.slice(0, 600)}……` : step.detail}
                    </pre>
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      ))}
    </div>
  );
}

// 审批队列：批准或驳回由审批人自己点，不经过模型
export function ApprovalPanel({ refreshKey, onDecided }: { refreshKey: number; onDecided: () => void }) {
  const [requests, setRequests] = useState<PendingApproval[]>([]);
  const [rejecting, setRejecting] = useState<number | null>(null);
  const [comment, setComment] = useState("");
  const [message, setMessage] = useState("");

  async function load() {
    setRequests(await api.approvals());
  }

  useEffect(() => {
    load();
  }, [refreshKey]);

  async function decide(requestId: number, decision: "approve" | "reject") {
    try {
      const result = await api.decide(requestId, decision, decision === "reject" ? comment : "");
      const texts: Record<string, string> = {
        approved: `已批准，${result.system_name} 权限已开通。`,
        rejected: "已驳回，申请人会收到邮件通知。",
        provision_failed: "已批准，但开通失败，已自动给 IT 二线建单处理。",
        pending: `已批准，下一级由 ${result.approver_name} 审批。`,
      };
      setMessage(texts[result.status] ?? result.status);
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "操作失败");
    }
    setRejecting(null);
    setComment("");
    await load();
    onDecided();
  }

  return (
    <div className="space-y-3 p-4">
      {message && <p className="rounded-lg bg-emerald-50 p-2 text-sm text-emerald-700">{message}</p>}
      {requests.length === 0 && <p className="text-sm text-slate-500">当前没有需要你审批的申请。</p>}
      {requests.map((request) => (
        <div key={request.request_id} className="rounded-xl bg-white p-3 shadow-sm ring-1 ring-slate-200">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-slate-900">
              {request.requester_name}（{request.department}）申请 {request.system_name}
            </p>
            <Badge tone={request.sensitivity === "高敏" ? "red" : "amber"}>{request.sensitivity}</Badge>
          </div>
          <p className="mt-1 text-sm text-slate-600">理由：{request.reason}</p>
          <p className="mt-0.5 text-xs text-slate-400">
            申请 {request.request_id} · {request.created_at} 提交
          </p>
          {rejecting === request.request_id ? (
            <div className="mt-2 space-y-2">
              <Input placeholder="驳回理由（会发给申请人）" value={comment} onChange={(event) => setComment(event.target.value)} />
              <div className="flex justify-end gap-2">
                <Button variant="secondary" onClick={() => setRejecting(null)}>
                  取消
                </Button>
                <Button variant="danger" onClick={() => decide(request.request_id, "reject")}>
                  确认驳回
                </Button>
              </div>
            </div>
          ) : (
            <div className="mt-2 flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setRejecting(request.request_id)}>
                驳回
              </Button>
              <Button onClick={() => decide(request.request_id, "approve")}>批准</Button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// 审计日志：IT 管理员看全部，其他人只看自己的
export function AuditPanel({ refreshKey, everyone }: { refreshKey: number; everyone: boolean }) {
  const [rows, setRows] = useState<AuditRow[]>([]);

  useEffect(() => {
    api.audit().then(setRows);
  }, [refreshKey]);

  return (
    <div className="p-4">
      <p className="mb-2 text-xs text-slate-500">
        {everyone ? "IT 管理员可看全部人的记录" : "只显示你自己的记录"} · 审计表只能追加，改和删都会被数据库触发器拒绝
      </p>
      <div className="space-y-1.5">
        {rows.map((row) => (
          <div key={row.id} className="rounded-lg bg-white px-3 py-2 text-xs shadow-sm ring-1 ring-slate-200">
            <div className="flex items-center gap-2">
              <span className="font-mono text-slate-400">{row.created_at.slice(5)}</span>
              <span className="font-medium text-slate-700">{EVENT_LABELS[row.event] ?? row.event}</span>
              {row.target && <span className="text-slate-600">{toolLabel(row.target)}</span>}
              {row.decision && <Badge tone={DECISION_TONES[row.decision] ?? "slate"}>{row.decision}</Badge>}
              <span className="ml-auto font-mono text-slate-400">{row.actor_id}</span>
            </div>
            {row.detail && row.event !== "llm_call" && (
              <p className="mt-1 truncate text-slate-500">{JSON.stringify(row.detail)}</p>
            )}
            {row.detail && row.event === "llm_call" && (
              <p className="mt-1 text-slate-500">
                token 输入 {String(row.detail.input_tokens)} / 输出 {String(row.detail.output_tokens)}
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
