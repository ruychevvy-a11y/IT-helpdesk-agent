import pytest

from helpdesk.iam import permissions
from helpdesk.infra import db

# 三种身份的 actor，和种子库里一致
EMPLOYEE = {"id": "E1007", "role": "employee", "department": "研发"}
MANAGER = {"id": "E1006", "role": "manager", "department": "研发"}
IT_ADMIN = {"id": "E1002", "role": "it_admin", "department": "IT"}


# 工具权限矩阵，表里没有的一律拒绝
@pytest.mark.parametrize(
    "role, tool_name, expected",
    [
        ("employee", "query_ticket", True),  # 正常
        ("employee", "unlock_account", False),  # employee无unlock_account
        ("manager", "unlock_account", False),  # manager无unlock_account
        ("it_admin", "unlock_account", True),  # it_admin有unlock_account
        ("employee", "delete_database", False),  # 表中没有的工具
        ("admin", "query_ticket", False),  # 不存在的角色admin
    ],
)
def test_is_tool_allowed(role, tool_name, expected):
    assert permissions.is_tool_allowed(role, tool_name) is expected


# 策略表只出现已知的角色和数据范围
def test_policy_table_only_uses_known_roles_and_scopes():
    for scopes in permissions.TOOL_POLICIES.values():
        assert set(scopes) <= {"employee", "manager", "it_admin"}
        assert set(scopes.values()) <= {"self", "department", "all"}


# 数据范围，谁在操作 + 数据属于谁 --> 能不能动
@pytest.mark.parametrize(
    "actor, tool_name, owner_id, owner_department, expected",
    [
        (EMPLOYEE, "query_ticket", "E1007", "研发", True),  # 员工看自己的
        (EMPLOYEE, "query_ticket", "E1011", "研发", False),  # 员工看同部门同事的
        (MANAGER, "query_ticket", "E1011", "研发", True),  # 经理看本部门的
        (MANAGER, "query_ticket", "E1020", "财务", False),  # 经理看别的部门的
        (MANAGER, "request_access", "E1007", "研发", False),  # 经理替下属申请权限
        (IT_ADMIN, "unlock_account", "E1007", "研发", True),  # IT 管理员解锁别人的
    ],
)
def test_is_in_scope(actor, tool_name, owner_id, owner_department, expected):
    result = permissions.is_in_scope(actor, tool_name, owner_id, owner_department)
    assert result is expected


# 没有这个工具的权限就判断不了范围
def test_is_in_scope_requires_permission():
    with pytest.raises(KeyError):
        permissions.is_in_scope(EMPLOYEE, "unlock_account", "E1008", "研发")


# 审批链的各种情况
@pytest.mark.parametrize(
    "requester_id, system_id, expected_chain",
    [
        ("E1007", "email", []),
        ("E1007", "crm", ["E1006"]),
        ("E1027", "sap", ["E1026", "E1019"]),
        ("E1020", "sap", ["E1019"]),
        ("E1005", "prod_servers", ["E1002"]),
        ("E1023", "hr_system", ["E1001"]),
        ("E1001", "gitlab", ["E1006"]),
    ],
)
def test_approval_chain(requester_id, system_id, expected_chain):
    with db.transaction() as connection:
        chain = permissions.approval_chain(connection, requester_id, system_id)
    assert chain == expected_chain


# 找不到审批人时报错，不放行
def test_empty_approval_chain_raises():
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO systems (id, name, sensitivity, owner_id) "
            "VALUES ('board', '董事会系统', 'medium', 'E1001')"
        )
        with pytest.raises(ValueError):
            permissions.approval_chain(connection, "E1001", "board")


# 确认说明：中敏、高敏申请要提交审批才问，低敏和不存在的系统不问；解锁带上姓名，工号不存在不问
@pytest.mark.parametrize(
    "tool_name, args, expected",
    [
        (
            "request_access",
            {"system_id": "crm", "reason": "跟进客户"},
            "CRM 是中敏系统，需要提交审批。申请理由：跟进客户",
        ),
        (
            "request_access",
            {"system_id": "sap", "reason": "查回款"},
            "财务 SAP 是高敏系统，需要提交审批。申请理由：查回款",
        ),
        ("request_access", {"system_id": "vpn", "reason": "居家办公"}, None),
        ("request_access", {"system_id": "github", "reason": "x"}, None),
        (
            "unlock_account",
            {"employee_id": "E1009"},
            "即将解锁 吴俊杰（E1009）的账号。",
        ),
        ("unlock_account", {"employee_id": "E9999"}, None),
    ],
)
def test_confirmation(tool_name, args, expected):
    with db.transaction() as connection:
        assert permissions.CONFIRM_TOOLS[tool_name](connection, args) == expected
