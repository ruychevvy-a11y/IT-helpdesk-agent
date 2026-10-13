import json
import re

import pytest
from argon2.exceptions import InvalidHashError

from helpdesk import config
from helpdesk.iam import auth
from helpdesk.infra import db

# 种子库里所有人的初始密码
INITIAL_PASSWORD = json.loads(config.SEED_PATH.read_text(encoding="utf-8"))[
    "initial_password"
]


# 查员工的登录状态
def login_state(employee_id):
    with db.transaction() as connection:
        return dict(
            connection.execute(
                "SELECT failed_logins, coalesce(locked_until > datetime('now'), 0) "
                "AS locked, session_version FROM employees WHERE id = ?",
                (employee_id,),
            ).fetchone()
        )


# 查审计日志
def audit_rows(event):
    with db.transaction() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT actor_id, target, decision, detail FROM audit_log "
                "WHERE event = ? ORDER BY id",
                (event,),
            )
        ]


# 测相同的密码hash不同
def test_same_password_hashes_differently():
    first_hash = auth.hash_password("Welcome-2026")
    second_hash = auth.hash_password("Welcome-2026")
    assert first_hash != second_hash
    assert auth.verify_password(first_hash, "Welcome-2026")
    assert auth.verify_password(second_hash, "Welcome-2026")


# 测错误密码和坏哈希是否能判断正确以及跳出error
def test_verify_password_rejects_wrong_and_raises_on_broken_hash():
    password_hash = auth.hash_password("Welcome-2026")
    assert auth.verify_password(password_hash, "welcome-2026") is False
    with pytest.raises(InvalidHashError):
        auth.verify_password("not-a-hash", "Welcome-2026")


# 测试登录信息以及审计日志是否缺失
def test_login_success_returns_profile():
    assert auth.authenticate("E1007", INITIAL_PASSWORD) == (
        "success",
        {"id": "E1007", "name": "李娜", "role": "employee", "session_version": 0},
    )
    assert audit_rows("login") == [
        {"actor_id": "E1007", "target": None, "decision": "success", "detail": None}
    ]


# 测试工号不存在和工号在密码不一样的返回值一样；工号不存在空跑了校验；审计里能区分
def test_unknown_employee(monkeypatch):
    verified_hashes = []
    original_verify = auth.verify_password

    # 记下校验时用的哈希
    def recording_verify(password_hash, password):
        verified_hashes.append(password_hash)
        return original_verify(password_hash, password)

    monkeypatch.setattr(auth, "verify_password", recording_verify)
    assert auth.authenticate("E9999", INITIAL_PASSWORD) == ("failure", None)
    assert auth.authenticate("E1007", "wrong-password") == ("failure", None)
    assert verified_hashes[0] == auth.DUMMY_PASSWORD_HASH
    assert [row["detail"] for row in audit_rows("login")] == [
        '{"reason": "unknown_employee"}',
        '{"reason": "wrong_password", "failed_logins": 1}',
    ]


# 测试输错次数达上限后账号是否锁定
def test_account_locks_after_max_failed_logins():
    for _ in range(auth.MAX_FAILED_LOGINS - 1):
        assert auth.authenticate("E1007", "wrong-password") == ("failure", None)
    assert auth.authenticate("E1007", "wrong-password") == ("locked", None)
    assert auth.authenticate("E1007", INITIAL_PASSWORD) == ("locked", None)
    assert login_state("E1007")["failed_logins"] == auth.MAX_FAILED_LOGINS
    assert login_state("E1007")["locked"] == 1


# 锁定到点自动解开；没输对过，再错一次马上又锁；输对一次才清零
def test_lockout_expires_and_relocks():
    for _ in range(auth.MAX_FAILED_LOGINS):
        auth.authenticate("E1007", "wrong-password")
    expire_lockout("E1007")
    assert auth.authenticate("E1007", "wrong-password") == ("locked", None)
    expire_lockout("E1007")
    assert auth.authenticate("E1007", INITIAL_PASSWORD)[0] == "success"
    assert login_state("E1007")["failed_logins"] == 0


# 把锁定时间改到过去，模拟锁定期已过
def expire_lockout(employee_id):
    with db.transaction() as connection:
        connection.execute(
            "UPDATE employees SET locked_until = datetime('now', '-1 minute') "
            "WHERE id = ?",
            (employee_id,),
        )


# 测成功输入密码后，输错次数是否清零
def test_success_resets_failed_logins():
    auth.authenticate("E1007", "wrong-password")
    auth.authenticate("E1007", "wrong-password")
    assert login_state("E1007")["failed_logins"] == 2
    auth.authenticate("E1007", INITIAL_PASSWORD)
    assert login_state("E1007")["failed_logins"] == 0


# 测试审计失败时整块回滚
def test_failed_audit_rolls_back_login_state(monkeypatch):
    # 审计写不进去
    def broken_audit(*args, **kwargs):
        raise RuntimeError("审计写入失败")

    monkeypatch.setattr(db, "record_audit", broken_audit)
    with pytest.raises(RuntimeError):
        auth.authenticate("E1007", "wrong-password")
    assert login_state("E1007")["failed_logins"] == 0


# 发件箱里最后一封邮件的验证码
def last_reset_code():
    last_email = config.OUTBOX_PATH.read_text(encoding="utf-8").splitlines()[-1]
    return re.search(r"验证码 (\d{6})", last_email).group(1)


# 发验证码不动账号，旧密码照样能用；凭验证码改密后旧密码失效，验证码只能用一次
def test_password_reset_with_code():
    auth.request_password_reset("E1007")
    assert config.OUTBOX_PATH.read_text(encoding="utf-8").startswith("E1007\t")
    assert auth.authenticate("E1007", INITIAL_PASSWORD)[0] == "success"
    code = last_reset_code()
    assert auth.reset_password_with_code("E1007", code, "blue-kettle-2026") == 1
    assert auth.authenticate("E1007", INITIAL_PASSWORD) == ("failure", None)
    assert auth.authenticate("E1007", "blue-kettle-2026")[0] == "success"
    with pytest.raises(ValueError, match="验证码错误或已过期"):
        auth.reset_password_with_code("E1007", code, "green-kettle-2026")
    assert [row["decision"] for row in audit_rows("password_reset")] == [
        "success",
        "failure",
    ]


# 工号不存在、一分钟内重复申请，都不发邮件也不报错；工号不存在也照样算哈希
def test_request_reset_unknown_or_too_frequent(monkeypatch):
    hashed_codes = []
    original_hash = auth.hash_password

    # 记下算过哈希的验证码
    def recording_hash(password):
        hashed_codes.append(password)
        return original_hash(password)

    monkeypatch.setattr(auth, "hash_password", recording_hash)
    auth.request_password_reset("E9999")
    auth.request_password_reset("E1007")
    auth.request_password_reset("E1007")
    assert len(hashed_codes) == 3
    assert len(config.OUTBOX_PATH.read_text(encoding="utf-8").splitlines()) == 1
    assert [row["detail"] for row in audit_rows("password_reset_request")] == [
        '{"reason": "unknown_employee"}',
        None,
        '{"reason": "too_frequent"}',
    ]


# 输错到上限验证码作废，对的也不行了；过期的也不行
def test_reset_code_wrong_too_many_times_or_expired():
    auth.request_password_reset("E1007")
    code = last_reset_code()
    wrong_code = "000000" if code != "000000" else "111111"
    for _ in range(auth.MAX_RESET_CODE_ATTEMPTS):
        with pytest.raises(ValueError):
            auth.reset_password_with_code("E1007", wrong_code, "blue-kettle-2026")
    with pytest.raises(ValueError):
        auth.reset_password_with_code("E1007", code, "blue-kettle-2026")

    auth.request_password_reset("E1008")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE password_reset_codes SET expires_at = datetime('now', '-1 minute')"
        )
    with pytest.raises(ValueError):
        auth.reset_password_with_code("E1008", last_reset_code(), "blue-kettle-2026")
    assert auth.authenticate("E1007", INITIAL_PASSWORD)[0] == "success"
    assert auth.authenticate("E1008", INITIAL_PASSWORD)[0] == "success"


# 新密码太短直接拒绝，验证码不作废；重置成功顺带解锁
def test_reset_checks_length_and_unlocks_account():
    for _ in range(auth.MAX_FAILED_LOGINS):
        auth.authenticate("E1007", "wrong-password")
    auth.request_password_reset("E1007")
    code = last_reset_code()
    with pytest.raises(ValueError, match="新密码至少"):
        auth.reset_password_with_code("E1007", code, "short")
    auth.reset_password_with_code("E1007", code, "blue-kettle-2026")
    assert login_state("E1007") == {
        "failed_logins": 0,
        "locked": 0,
        "session_version": 1,
    }
    assert auth.authenticate("E1007", "blue-kettle-2026")[0] == "success"
    assert "已重置" in config.OUTBOX_PATH.read_text(encoding="utf-8").splitlines()[-1]


# 会话版本一致才算登录有效；密码被重置过、工号查不到都让登录失效
def test_load_actor_checks_session():
    actor = auth.load_actor(auth.Context(user_id="E1007", session_version=0))
    assert actor["name"] == "李娜"
    for context in (
        auth.Context(user_id="E1007", session_version=1),
        auth.Context(user_id="E9999", session_version=0),
    ):
        with pytest.raises(auth.SessionExpired):
            auth.load_actor(context)


# 发验证码的事务回滚了，邮件不发出去
def test_reset_code_not_sent_when_rolled_back(monkeypatch):
    def broken_record_audit(*args, **kwargs):
        raise RuntimeError("审计写入失败")

    monkeypatch.setattr(db, "record_audit", broken_record_audit)
    with pytest.raises(RuntimeError):
        auth.request_password_reset("E1007")
    assert not config.OUTBOX_PATH.exists()
