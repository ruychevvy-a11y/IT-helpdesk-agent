import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from helpdesk import config
from helpdesk.iam import approval, auth
from helpdesk.infra import db
from helpdesk.tools import access

# 种子库里的经理王海涛，李娜（E1007）的直属经理
WANG_HAITAO = auth.Context(user_id="E1006", session_version=0)

# 冯晓雪（E1027）申请 SAP 的两级审批人，直属经理唐亮、系统负责人郭红梅
TANG_LIANG = auth.Context(user_id="E1026", session_version=0)
GUO_HONGMEI = auth.Context(user_id="E1019", session_version=0)


# 查一条申请
def access_request(request_id):
    with db.transaction() as connection:
        return dict(
            connection.execute(
                "SELECT status, current_approver_id FROM access_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        )


# 审批线程停在哪个节点，没有线程或已结束都是空
def waiting_node(request_id):
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB_PATH)) as checkpointer:
        graph = approval.build_approval_graph(checkpointer)
        return graph.get_state(approval.approval_config(request_id)).next


# 审批和开通的审计
def approval_audit():
    with db.transaction() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT event, actor_id, decision FROM audit_log "
                "WHERE event IN ('approval', 'provision') ORDER BY id"
            )
        ]


# 李娜申请 CRM（中敏，直属经理王海涛审批），返回申请号
def request_crm(call_tool):
    result = call_tool(
        access.request_access, "E1007", system_id="crm", reason="跟进客户"
    )
    return result["request_id"]


# 冯晓雪申请财务 SAP（高敏，两级审批），返回申请号
def request_sap(call_tool):
    result = call_tool(access.request_access, "E1027", system_id="sap", reason="查回款")
    return result["request_id"]


# 冯晓雪当前有效的权限
def granted_systems(call_tool):
    grants = call_tool(access.query_access, "E1027")["grants"]
    return [grant["system_id"] for grant in grants]


# 发件箱里的邮件，每行 (工号, 内容)
def outbox():
    if not config.OUTBOX_PATH.exists():
        return []
    return [
        tuple(line.split("\t", 1))
        for line in config.OUTBOX_PATH.read_text(encoding="utf-8").splitlines()
    ]


# 提交成功才执行回调，回滚就丢掉
def test_after_commit_runs_only_after_commit():
    called = []
    with db.transaction() as connection:
        connection.after_commit.append(lambda: called.append("committed"))
        assert called == []
    assert called == ["committed"]
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            connection.after_commit.append(lambda: called.append("rolled back"))
            raise RuntimeError
    assert called == ["committed"]


# 中敏申请提交后启动审批线程，停在第一级等审批
def test_request_starts_approval_thread(call_tool):
    request_id = request_crm(call_tool)
    assert waiting_node(request_id) == ("review",)
    assert access_request(request_id) == {
        "status": "pending",
        "current_approver_id": "E1006",
    }


# 申请的事务回滚，申请和审批线程都不留下
def test_rolled_back_request_starts_no_thread():
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            actor = connection.execute(
                "SELECT id, role, department FROM employees WHERE id = 'E1007'"
            ).fetchone()
            result = access.request_access.invoke(
                {
                    "system_id": "crm",
                    "reason": "跟进客户",
                    "actor": actor,
                    "connection": connection,
                }
            )
            raise RuntimeError
    assert waiting_node(result["request_id"]) == ()
    with db.transaction() as connection:
        count = connection.execute("SELECT count(*) FROM access_requests").fetchone()
    assert count[0] == 0


# 经理批准中敏申请，开通权限、申请改成 approved、线程结束、记审计
def test_manager_approves_medium_request(call_tool):
    request_id = request_crm(call_tool)
    request = approval.decide_approval(request_id, WANG_HAITAO, "approve")
    assert request == {
        "status": "approved",
        "system_name": "CRM",
        "approver_name": None,
    }
    granted = call_tool(access.query_access, "E1007")["grants"]
    assert "crm" in [grant["system_id"] for grant in granted]
    assert waiting_node(request_id) == ()
    assert approval_audit() == [
        {"event": "approval", "actor_id": "E1006", "decision": "approve"},
        {"event": "provision", "actor_id": None, "decision": "success"},
    ]


# 不归自己审批的申请，恢复前就拒绝，什么都不改
def test_decide_rejects_non_approver(call_tool):
    request_id = request_crm(call_tool)
    with pytest.raises(ValueError, match="非你审核"):
        approval.decide_approval(
            request_id, auth.Context(user_id="E1008", session_version=0), "approve"
        )
    assert access_request(request_id)["current_approver_id"] == "E1006"
    assert approval_audit() == []


# 绕过检查直接恢复线程，节点核对恢复者，不对就记 deny、继续等
def test_wrong_resumer_keeps_waiting(call_tool):
    request_id = request_crm(call_tool)
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB_PATH)) as checkpointer:
        approval.build_approval_graph(checkpointer).invoke(
            Command(resume="approve"),
            approval.approval_config(request_id),
            context=auth.Context(user_id="E1008", session_version=0),
        )
    assert waiting_node(request_id) == ("review",)
    assert access_request(request_id)["status"] == "pending"
    assert approval_audit() == [
        {"event": "approval", "actor_id": "E1008", "decision": "deny"}
    ]
    request = approval.decide_approval(request_id, WANG_HAITAO, "approve")
    assert request["status"] == "approved"


# 申请没有审批线程时不恢复，免得从头新开一条空线程
def test_decide_without_thread():
    with db.transaction() as connection:
        request_id = connection.execute(
            "INSERT INTO access_requests "
            "(requester_id, system_id, reason, current_approver_id) "
            "VALUES ('E1007', 'crm', '跟进客户', 'E1006') RETURNING id"
        ).fetchone()[0]
    with pytest.raises(ValueError, match="审批线程没在等审批"):
        approval.decide_approval(request_id, WANG_HAITAO, "approve")
    assert waiting_node(request_id) == ()


# 审批人的密码在别处被重置过，恢复时登录失效，申请不动
def test_decide_with_expired_session(call_tool):
    request_id = request_crm(call_tool)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE employees SET session_version = 1 WHERE id = 'E1006'"
        )
    with pytest.raises(auth.SessionExpired):
        approval.decide_approval(request_id, WANG_HAITAO, "approve")
    assert access_request(request_id)["current_approver_id"] == "E1006"


# 已有过期的授权，开通后覆盖成新的到期时间，按系统的有效天数算
def test_grant_access_renews_expired_grant():
    with db.transaction() as connection:
        approval.grant_access(connection, "E1009", "prod_servers")
        days_left = connection.execute(
            "SELECT julianday(expires_at) - julianday('now') FROM grants "
            "WHERE employee_id = 'E1009' AND system_id = 'prod_servers'"
        ).fetchone()[0]
    assert 6.99 < days_left <= 7


# 待审批列表只列轮到自己的、还在等的申请
def test_pending_approvals(call_tool):
    request_id = request_crm(call_tool)
    pending = approval.list_pending_approvals("E1006")
    assert [
        {key: request[key] for key in ("request_id", "requester_name", "system_name")}
        for request in pending
    ] == [{"request_id": request_id, "requester_name": "李娜", "system_name": "CRM"}]
    assert pending[0]["sensitivity"] == "medium" and pending[0]["reason"] == "跟进客户"
    assert approval.list_pending_approvals("E1008") == []
    approval.decide_approval(request_id, WANG_HAITAO, "approve")
    assert approval.list_pending_approvals("E1006") == []


# 高敏两级审批，第一级批完等系统负责人，第二级批完才开通
def test_two_level_approval(call_tool):
    request_id = request_sap(call_tool)
    request = approval.decide_approval(request_id, TANG_LIANG, "approve")
    assert request == {
        "status": "pending",
        "system_name": "财务 SAP",
        "approver_name": "郭红梅",
    }
    assert waiting_node(request_id) == ("review",)
    assert "sap" not in granted_systems(call_tool)
    request = approval.decide_approval(request_id, GUO_HONGMEI, "approve")
    assert request["status"] == "approved"
    assert "sap" in granted_systems(call_tool)
    assert waiting_node(request_id) == ()
    assert approval_audit() == [
        {"event": "approval", "actor_id": "E1026", "decision": "approve"},
        {"event": "approval", "actor_id": "E1019", "decision": "approve"},
        {"event": "provision", "actor_id": None, "decision": "success"},
    ]


# 第一级驳回，申请结束、不开通，审批人的列表里没有了
def test_reject_at_first_level(call_tool):
    request_id = request_crm(call_tool)
    request = approval.decide_approval(request_id, WANG_HAITAO, "reject")
    assert request == {
        "status": "rejected",
        "system_name": "CRM",
        "approver_name": None,
    }
    assert access_request(request_id) == {
        "status": "rejected",
        "current_approver_id": None,
    }
    assert waiting_node(request_id) == ()
    assert approval.list_pending_approvals("E1006") == []
    assert approval_audit() == [
        {"event": "approval", "actor_id": "E1006", "decision": "reject"}
    ]


# 第一级批准后第二级驳回，同样不开通
def test_reject_at_second_level(call_tool):
    request_id = request_sap(call_tool)
    approval.decide_approval(request_id, TANG_LIANG, "approve")
    request = approval.decide_approval(request_id, GUO_HONGMEI, "reject")
    assert request["status"] == "rejected"
    assert "sap" not in granted_systems(call_tool)
    assert waiting_node(request_id) == ()
    assert [row["decision"] for row in approval_audit()] == ["approve", "reject"]


# 批准、驳回以外的决定值不碰线程，申请原样等着
def test_decide_rejects_unknown_decision(call_tool):
    request_id = request_crm(call_tool)
    with pytest.raises(ValueError, match="不支持的审批决定"):
        approval.decide_approval(request_id, WANG_HAITAO, "yes")
    assert waiting_node(request_id) == ("review",)
    assert access_request(request_id)["status"] == "pending"
    assert approval_audit() == []


# 绕过入口直接用别的决定值恢复线程，节点不当成批准，申请原样等着
def test_review_ignores_unknown_decision(call_tool):
    request_id = request_crm(call_tool)
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB_PATH)) as checkpointer:
        approval.build_approval_graph(checkpointer).invoke(
            Command(resume={"decision": "yes", "comment": ""}),
            approval.approval_config(request_id),
            context=WANG_HAITAO,
        )
    assert waiting_node(request_id) == ("review",)
    assert access_request(request_id) == {
        "status": "pending",
        "current_approver_id": "E1006",
    }
    assert approval_audit() == []


# 开通出错，授权整块回滚；申请标成开通失败，给二线建单，记审计，线程照常结束
def test_provision_failure_creates_ticket(monkeypatch, call_tool):
    request_id = request_crm(call_tool)

    def broken_grant_access(connection, employee_id, system_id):
        raise RuntimeError("CRM 接口超时")

    monkeypatch.setattr(approval, "grant_access", broken_grant_access)
    request = approval.decide_approval(request_id, WANG_HAITAO, "approve")
    assert request["status"] == "provision_failed"
    assert "crm" not in [
        grant["system_id"]
        for grant in call_tool(access.query_access, "E1007")["grants"]
    ]
    with db.transaction() as connection:
        created = dict(
            connection.execute(
                "SELECT requester_id, ticket_type, title, description, priority, "
                "assignee_group FROM tickets ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )
    assert created["requester_id"] == "E1007"
    assert created["ticket_type"] == "service_request"
    assert created["title"] == "权限开通失败：CRM"
    assert "CRM 接口超时" in created["description"]
    assert (created["priority"], created["assignee_group"]) == ("P4", "L2")
    assert waiting_node(request_id) == ()
    assert [row["decision"] for row in approval_audit()] == ["approve", "failure"]


# 写完库、存检查点前崩溃，重跑节点时比较后更新 0 行，不重复推进、不重复记审计
def test_rerun_after_crash_is_idempotent(monkeypatch, call_tool):
    request_id = request_crm(call_tool)
    real_command = approval.Command
    crashed = []

    # 第一次往下一个节点走时抛异常，相当于库已经提交、检查点还没存
    def crash_once(**kwargs):
        if "goto" in kwargs and not crashed:
            crashed.append(True)
            raise RuntimeError("进程崩溃")
        return real_command(**kwargs)

    monkeypatch.setattr(approval, "Command", crash_once)
    with pytest.raises(RuntimeError, match="进程崩溃"):
        approval.decide_approval(request_id, WANG_HAITAO, "approve")
    assert access_request(request_id) == {
        "status": "pending",
        "current_approver_id": None,
    }
    # 库里已经推进，原审批人走入口重试会被挡住，只能由恢复流程重跑
    with pytest.raises(ValueError, match="非你审核"):
        approval.decide_approval(request_id, WANG_HAITAO, "approve")
    # 用原审批人的身份重跑出错的节点，恢复值还存在检查点里
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB_PATH)) as checkpointer:
        approval.build_approval_graph(checkpointer).invoke(
            None, approval.approval_config(request_id), context=WANG_HAITAO
        )
    assert access_request(request_id)["status"] == "approved"
    assert "crm" in [
        grant["system_id"]
        for grant in call_tool(access.query_access, "E1007")["grants"]
    ]
    assert approval_audit() == [
        {"event": "approval", "actor_id": "E1006", "decision": "approve"},
        {"event": "provision", "actor_id": None, "decision": "success"},
    ]


# 开通节点重跑，状态已经不是 pending，不再授权、不再记审计
def test_provision_rerun_does_nothing(call_tool):
    request_id = request_crm(call_tool)
    approval.decide_approval(request_id, WANG_HAITAO, "approve")
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB_PATH)) as checkpointer:
        graph = approval.build_approval_graph(checkpointer)
        provision = graph.nodes["provision"].bound
        provision.invoke(
            {
                "request_id": request_id,
                "requester_id": "E1007",
                "system_id": "crm",
                "approval_chain": ["E1006"],
                "level": 0,
            }
        )
    assert [row["event"] for row in approval_audit()] == ["approval", "provision"]


# 批准开通后通知申请人
def test_approval_notifies_requester(call_tool):
    request_id = request_crm(call_tool)
    approval.decide_approval(request_id, WANG_HAITAO, "approve")
    assert outbox() == [("E1007", f"你申请的 CRM 权限已开通（申请 {request_id}）。")]


# 驳回时带上理由通知申请人，理由也记进审计；不填理由就不写理由
def test_rejection_notifies_requester_with_comment(call_tool):
    request_id = request_crm(call_tool)
    approval.decide_approval(request_id, WANG_HAITAO, "reject", "没有业务需要")
    assert outbox() == [
        (
            "E1007",
            f"你申请的 CRM 权限被 王海涛 驳回，理由：没有业务需要（申请 {request_id}）。",
        )
    ]
    with db.transaction() as connection:
        detail = connection.execute(
            "SELECT detail FROM audit_log WHERE event = 'approval'"
        ).fetchone()[0]
    assert "没有业务需要" in detail
    second_id = request_crm(call_tool)
    approval.decide_approval(second_id, WANG_HAITAO, "reject")
    with db.transaction() as connection:
        reasons = connection.execute(
            "SELECT reject_reason FROM access_requests ORDER BY id"
        ).fetchall()
    assert [row[0] for row in reasons] == ["没有业务需要", None]
    assert outbox()[-1] == (
        "E1007",
        f"你申请的 CRM 权限被 王海涛 驳回（申请 {second_id}）。",
    )


# 开通失败也通知申请人，带上补偿工单号
def test_provision_failure_notifies_requester(monkeypatch, call_tool):
    request_id = request_crm(call_tool)

    def broken_grant_access(connection, employee_id, system_id):
        raise RuntimeError("CRM 接口超时")

    monkeypatch.setattr(approval, "grant_access", broken_grant_access)
    approval.decide_approval(request_id, WANG_HAITAO, "approve")
    with db.transaction() as connection:
        ticket_id = connection.execute("SELECT max(id) FROM tickets").fetchone()[0]
    assert outbox() == [
        (
            "E1007",
            f"你申请的 CRM 权限审批已通过，但开通失败，IT 已建工单 {ticket_id} 处理"
            f"（申请 {request_id}）。",
        )
    ]


# 两级审批批完第一级不通知，开通了才通知
def test_first_level_approval_sends_nothing(call_tool):
    request_id = request_sap(call_tool)
    approval.decide_approval(request_id, TANG_LIANG, "approve")
    assert outbox() == []
