import json
from pathlib import Path

from helpdesk import config
from helpdesk.iam import auth
from helpdesk.infra import db


# 重建数据库并灌入种子数据
def seed_database():
    seed = json.loads(config.SEED_PATH.read_text(encoding="utf-8"))

    # 业务库和检查点库一起删，连同日志文件
    for database_path in (config.DB_PATH, config.CHECKPOINT_DB_PATH):
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{database_path}{suffix}").unlink(missing_ok=True)
    # 发件箱一起清空
    config.OUTBOX_PATH.unlink(missing_ok=True)
    db.create_tables()

    with db.transaction() as connection:
        # employees表，每人单独算哈希
        connection.executemany(
            "INSERT INTO employees (id, name, department, role, manager_id, password_hash) "
            "VALUES (:id, :name, :department, :role, :manager_id, :password_hash)",
            [
                {
                    **employee,
                    "password_hash": auth.hash_password(seed["initial_password"]),
                }
                for employee in seed["employees"]
            ],
        )
        # system表
        connection.executemany(
            "INSERT INTO systems (id, name, sensitivity, owner_id, grant_days) "
            "VALUES (:id, :name, :sensitivity, :owner_id, :grant_days)",
            seed["systems"],
        )

        # grants表，权限按照每个employee展开
        connection.executemany(
            "INSERT INTO grants (employee_id, system_id) "
            "VALUES (:employee_id, :system_id)",
            [
                {"employee_id": employee["id"], "system_id": system_id}
                for employee in seed["employees"]
                for system_id in seed["baseline_grants"][employee["department"]]
            ],
        )
        # 额外权限
        connection.executemany(
            "INSERT INTO grants (employee_id, system_id, granted_at, expires_at) "
            "VALUES (:employee_id, :system_id, :granted_at, :expires_at)",
            seed["extra_grants"],
        )

        # tickets表
        connection.executemany(
            "INSERT INTO tickets (requester_id, ticket_type, title, description, "
            "impact, urgency, priority, status, assignee_group, created_at) "
            "VALUES (:requester_id, :ticket_type, :title, :description, "
            ":impact, :urgency, :priority, :status, :assignee_group, :created_at)",
            seed["tickets"],
        )

    # 各表行数
    with db.transaction() as connection:
        for table in ("employees", "systems", "grants", "tickets"):
            count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"{table} --> {count}")


if __name__ == "__main__":
    seed_database()
