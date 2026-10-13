// 后端接口和对话事件流

export type Role = "employee" | "manager" | "it_admin";

export interface Me {
  id: string;
  name: string;
  role: Role;
  department: string;
  pending_approvals: number;
  commands: Record<string, string>;
}

export type ChatEvent =
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; name: string; status: "success" | "error"; content: string }
  | { type: "answer"; content: string }
  | { type: "confirm"; operations: string[] }
  | { type: "open_ui"; ui: "password_form" | "approval_list"; pending?: number }
  | { type: "error"; message: string }
  | { type: "expired"; message: string };

export interface PendingApproval {
  request_id: number;
  requester_name: string;
  department: string;
  system_name: string;
  sensitivity: string;
  reason: string;
  created_at: string;
}

export interface AuditRow {
  id: number;
  created_at: string;
  actor_id: string | null;
  thread_id: string | null;
  event: string;
  target: string | null;
  decision: string | null;
  detail: Record<string, unknown> | null;
}

// 登录令牌只放内存，刷新页面就要重新登录
let token = "";

export function setToken(value: string) {
  token = value;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...options.headers,
    },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, body.detail ?? `请求失败（${response.status}）`);
  }
  return body as T;
}

export const api = {
  login: (employee_id: string, password: string) =>
    request<{ token: string }>("/api/login", {
      method: "POST",
      body: JSON.stringify({ employee_id, password }),
    }),
  logout: () => request("/api/logout", { method: "POST" }),
  me: () => request<Me>("/api/me"),
  newChat: () => request("/api/chat/new", { method: "POST" }),
  passwordCode: () => request<{ message: string }>("/api/password/code", { method: "POST" }),
  forgotCode: (employee_id: string) =>
    request<{ message: string }>("/api/forgot/code", {
      method: "POST",
      body: JSON.stringify({ employee_id }),
    }),
  resetPassword: (employee_id: string, code: string, new_password: string) =>
    request<{ message: string }>("/api/password/reset", {
      method: "POST",
      body: JSON.stringify({ employee_id, code, new_password }),
    }),
  approvals: () => request<PendingApproval[]>("/api/approvals"),
  decide: (request_id: number, decision: "approve" | "reject", comment = "") =>
    request<{ status: string; system_name: string; approver_name: string | null }>(
      `/api/approvals/${request_id}`,
      { method: "POST", body: JSON.stringify({ decision, comment }) },
    ),
  audit: () => request<AuditRow[]>("/api/audit"),
  outbox: (employee_id: string) => request<string[]>(`/api/outbox/${employee_id}`),
};

// 发一段对话，按 SSE 逐条回调事件
async function streamEvents(path: string, body: unknown, onEvent: (event: ChatEvent) => void) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => ({}));
    throw new ApiError(response.status, detail.detail ?? "请求失败");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      if (part.startsWith("data: ")) onEvent(JSON.parse(part.slice(6)));
    }
  }
}

export const sendMessage = (message: string, onEvent: (event: ChatEvent) => void) =>
  streamEvents("/api/chat", { message }, onEvent);

export const sendConfirmation = (confirmed: boolean, onEvent: (event: ChatEvent) => void) =>
  streamEvents("/api/chat/confirm", { confirmed }, onEvent);

export const ROLE_LABELS: Record<Role, string> = {
  employee: "员工",
  manager: "经理",
  it_admin: "IT 管理员",
};
