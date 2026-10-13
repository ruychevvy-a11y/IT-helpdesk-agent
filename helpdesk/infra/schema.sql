-- 员工表，locked_until 为空表示没锁，session_version 在重置密码时加一，之前的登录随之失效
CREATE TABLE employees (
    id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    department TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('employee', 'manager', 'it_admin')),
    manager_id TEXT REFERENCES employees (id),
    password_hash TEXT NOT NULL,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    session_version INTEGER NOT NULL DEFAULT 0
);

-- 密码重置验证码，每人最多一条，新发的覆盖旧的，用掉或输错太多次就删
CREATE TABLE password_reset_codes (
    employee_id TEXT NOT NULL PRIMARY KEY REFERENCES employees (id),
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 业务系统，sensitivity 决定审批几级，grant_days 为权限有效天数，空则为长期授权
CREATE TABLE systems (
    id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low', 'medium', 'high')),
    owner_id TEXT NOT NULL REFERENCES employees (id),
    grant_days INTEGER
);

-- 访问申请，current_approver_id 是现在在等谁审，审完为空；reject_reason 是驳回理由，没驳回或没填为空
CREATE TABLE access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id TEXT NOT NULL REFERENCES employees (id),
    system_id TEXT NOT NULL REFERENCES systems (id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'provision_failed')),
    current_approver_id TEXT REFERENCES employees (id),
    reject_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 已有权限，同一人同一系统只有一行，expires_at 为空表示长期有效
CREATE TABLE grants (
    employee_id TEXT NOT NULL REFERENCES employees (id),
    system_id TEXT NOT NULL REFERENCES systems (id),
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    PRIMARY KEY (employee_id, system_id)
);

-- 工单，优先级 priority 由 impact 和 urgency 算出来
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id TEXT NOT NULL REFERENCES employees (id),
    ticket_type TEXT NOT NULL CHECK (ticket_type IN ('incident', 'service_request')),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    impact TEXT NOT NULL CHECK (impact IN ('high', 'medium', 'low')),
    urgency TEXT NOT NULL CHECK (urgency IN ('high', 'medium', 'low')),
    priority TEXT NOT NULL CHECK (priority IN ('P1', 'P2', 'P3', 'P4')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
    assignee_group TEXT NOT NULL DEFAULT 'L1' CHECK (assignee_group IN ('L1', 'L2')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 审计日志，detail 存 JSON（参数、结果、token 数）
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    actor_id TEXT,
    thread_id TEXT,
    event TEXT NOT NULL,
    target TEXT,
    decision TEXT,
    detail TEXT
);

-- 审计日志只能追加，改和删都直接报错
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log 只能追加');
END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log 只能追加');
END;
