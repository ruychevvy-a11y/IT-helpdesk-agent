import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher, profiles
from argon2.exceptions import VerifyMismatchError

from helpdesk.infra import db, notify

# 连续输错几次锁定账号
MAX_FAILED_LOGINS = 5

# 锁定几分钟，到点自动解锁
LOCKOUT_MINUTES = 15

# 新密码最短长度
MIN_PASSWORD_LENGTH = 10

# 重置验证码几分钟内有效
RESET_CODE_MINUTES = 5

# 验证码最多输错几次，到了就作废
MAX_RESET_CODE_ATTEMPTS = 5

# 验证码发送间隔
RESET_CODE_COOLDOWN_SECONDS = 60

# 密码哈希
password_hasher = PasswordHasher.from_parameters(profiles.RFC_9106_LOW_MEMORY)

# 工号不存在时空跑一次校验，让响应时间在有无工号的前后一样
DUMMY_PASSWORD_HASH = password_hasher.hash("dummy-password")


# 算哈希密码
def hash_password(password):
    return password_hasher.hash(password)


# 校验密码
def verify_password(password_hash, password):
    try:
        return password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


# 登录
def authenticate(employee_id, password):
    with db.transaction() as connection:
        employee = connection.execute(
            "SELECT id, name, role, password_hash, session_version, "
            "locked_until > datetime('now') AS locked "  # 是否locked
            "FROM employees WHERE id = ?",
            (employee_id,),
        ).fetchone()

        # 如果工号不存在，则空跑一次校验
        if employee is None:
            verify_password(DUMMY_PASSWORD_HASH, password)
            decision, detail = "failure", {"reason": "unknown_employee"}

        # 账号已锁定
        elif employee["locked"]:
            decision, detail = "locked", {"reason": "account_locked"}

        # 密码正确，失败次数清零
        elif verify_password(employee["password_hash"], password):
            connection.execute(
                "UPDATE employees SET failed_logins = 0 WHERE id = ?", (employee_id,)
            )
            decision, detail = "success", None

        # 密码错误，失败次数加一，到上限锁一段时间；输对清零，到点解锁后再错一次又会锁
        else:
            failed_logins = connection.execute(
                "UPDATE employees SET failed_logins = failed_logins + 1 "
                "WHERE id = ? RETURNING failed_logins",
                (employee_id,),
            ).fetchone()["failed_logins"]
            if failed_logins >= MAX_FAILED_LOGINS:
                connection.execute(
                    "UPDATE employees SET locked_until = datetime('now', ?) "
                    "WHERE id = ?",
                    (f"+{LOCKOUT_MINUTES} minutes", employee_id),
                )
                decision = "locked"
            else:
                decision = "failure"
            detail = {"reason": "wrong_password", "failed_logins": failed_logins}

        # 每次登录一次审计
        db.record_audit(
            connection, "login", actor_id=employee_id, decision=decision, detail=detail
        )

        if decision != "success":
            return decision, None
        return decision, {
            "id": employee["id"],
            "name": employee["name"],
            "role": employee["role"],
            "session_version": employee["session_version"],
        }


# 运行时上下文，存登录身份
@dataclass
class Context:
    user_id: str
    session_version: int


# 登录已失效
class SessionExpired(Exception):
    pass


# 查当前用户，查不到或会话版本不一致就让登录失效
def load_actor(context):
    with db.transaction() as connection:
        actor = connection.execute(
            "SELECT id, name, role, department, session_version "
            "FROM employees WHERE id = ?",
            (context.user_id,),
        ).fetchone()
    if actor is None or actor["session_version"] != context.session_version:
        raise SessionExpired
    return actor


# 发重置验证码到员工邮箱，工号不存在、发得太频繁都不报错，调用方回同一句话
def request_password_reset(employee_id):
    code = f"{secrets.randbelow(10**6):06d}"
    # 先算哈希，工号存不存在耗时都一样
    code_hash = hash_password(code)
    with db.transaction() as connection:
        employee = connection.execute(
            "SELECT id FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        # 查找60秒内是否有验证码
        recent_code = connection.execute(
            "SELECT created_at FROM password_reset_codes "
            "WHERE employee_id = ? AND created_at > datetime('now', ?)",
            (employee_id, f"-{RESET_CODE_COOLDOWN_SECONDS} seconds"),
        ).fetchone()

        if employee is None:
            decision, detail = "failure", {"reason": "unknown_employee"}
        elif recent_code is not None:
            decision, detail = "failure", {"reason": "too_frequent"}

        # 新验证码覆盖旧的，错误次数清零
        else:
            connection.execute(
                "INSERT INTO password_reset_codes (employee_id, code_hash, expires_at) "
                "VALUES (?, ?, datetime('now', ?)) "
                "ON CONFLICT (employee_id) DO UPDATE SET "
                "code_hash = excluded.code_hash, expires_at = excluded.expires_at, "
                "failed_attempts = 0, created_at = excluded.created_at",
                (employee_id, code_hash, f"+{RESET_CODE_MINUTES} minutes"),
            )
            # 提交后再发，回滚就不发
            connection.after_commit.append(
                lambda: notify.send_email(
                    employee_id,
                    f"IT账号密码重置验证码 {code}，{RESET_CODE_MINUTES} 分钟内有效。",
                )
            )
            decision, detail = "success", None

        db.record_audit(
            connection,
            "password_reset_request",
            actor_id=employee_id,
            decision=decision,
            detail=detail,
        )


# 凭验证码设新密码，成功后作废验证码，清掉失败次数和锁定
def reset_password_with_code(employee_id, code, new_password):
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"新密码至少 {MIN_PASSWORD_LENGTH} 位")
    with db.transaction() as connection:
        reset_code = connection.execute(
            "SELECT code_hash FROM password_reset_codes "
            "WHERE employee_id = ? AND expires_at > datetime('now')",
            (employee_id,),
        ).fetchone()
        # 验证码对了才有新的会话版本号
        session_version = None

        # 无有效验证码，空跑一次校验
        if reset_code is None:
            verify_password(DUMMY_PASSWORD_HASH, code)
            decision, detail = "failure", {"reason": "no_valid_code"}

        elif not verify_password(reset_code["code_hash"], code):
            # 输错次数加一
            connection.execute(
                "UPDATE password_reset_codes SET failed_attempts = failed_attempts + 1 "
                "WHERE employee_id = ?",
                (employee_id,),
            )
            # 到上限验证码作废
            connection.execute(
                "DELETE FROM password_reset_codes "
                "WHERE employee_id = ? AND failed_attempts >= ?",
                (employee_id, MAX_RESET_CODE_ATTEMPTS),
            )
            decision, detail = "failure", {"reason": "wrong_code"}

        # 验证码对了才改密码，同时解锁、让之前的登录全部失效，并通知本人
        else:
            session_version = connection.execute(
                "UPDATE employees SET password_hash = ?, failed_logins = 0, "
                "locked_until = NULL, session_version = session_version + 1 "
                "WHERE id = ? RETURNING session_version",
                (hash_password(new_password), employee_id),
            ).fetchone()["session_version"]
            connection.execute(
                "DELETE FROM password_reset_codes WHERE employee_id = ?", (employee_id,)
            )
            # 提交后再发，回滚就不发
            connection.after_commit.append(
                lambda: notify.send_email(
                    employee_id,
                    "IT账号密码已重置，之前的登录已全部失效，不是本人操作请立即联系 IT。",
                )
            )
            decision, detail = "success", None

        db.record_audit(
            connection,
            "password_reset",
            actor_id=employee_id,
            decision=decision,
            detail=detail,
        )
    # 先提交再报错，错误次数才记得下来
    if decision != "success":
        raise ValueError("验证码错误或已过期")
    return session_version
