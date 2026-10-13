// 工具名、网关结果、审计判定的中文说明和配色

export const TOOL_LABELS: Record<string, string> = {
  search_kb: "查知识库",
  query_access: "查系统权限",
  request_access: "申请权限",
  list_access_requests: "查申请进度",
  unlock_account: "解锁账号",
  open_password_form: "弹出改密码框",
  open_approval_list: "弹出审批列表",
  create_ticket: "建工单",
  query_ticket: "查工单",
  list_tickets: "列工单",
  handoff_to_human: "转人工",
};

export const toolLabel = (name: string) => TOOL_LABELS[name] ?? name;

// 网关回给模型的结果，按内容分成几种
export function resultKind(status: string, content: string) {
  if (status === "success") return { label: "已执行", tone: "green" } as const;
  if (content.includes("无权使用")) return { label: "网关拒绝", tone: "red" } as const;
  if (content.includes("用户取消")) return { label: "用户取消", tone: "slate" } as const;
  if (content.includes("已经执行过")) return { label: "重复拦下", tone: "slate" } as const;
  if (content.includes("无权")) return { label: "数据范围拦下", tone: "red" } as const;
  return { label: "执行出错", tone: "amber" } as const;
}

export const DECISION_TONES: Record<string, Tone> = {
  allow: "green",
  success: "green",
  approve: "green",
  deny: "red",
  failure: "red",
  locked: "red",
  reject: "red",
  error: "amber",
  duplicate: "slate",
  cancel: "slate",
};

export const EVENT_LABELS: Record<string, string> = {
  login: "登录",
  tool_call: "工具调用",
  llm_call: "模型调用",
  password_reset_request: "申请验证码",
  password_reset: "重置密码",
  approval: "审批",
  provision: "开通权限",
};

export type Tone = "green" | "red" | "amber" | "slate" | "indigo";

export const TONE_CLASSES: Record<Tone, string> = {
  green: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  red: "bg-rose-50 text-rose-700 ring-rose-600/20",
  amber: "bg-amber-50 text-amber-700 ring-amber-600/20",
  slate: "bg-slate-100 text-slate-600 ring-slate-500/20",
  indigo: "bg-indigo-50 text-indigo-700 ring-indigo-600/20",
};
