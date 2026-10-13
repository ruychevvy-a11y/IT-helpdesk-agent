import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from helpdesk import config

# 建表语句，放在同目录的 schema.sql 里
SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


# 带提交后回调的连接
class Connection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.after_commit = []


# 事务连接
@contextmanager
def transaction():
    # mode=rw 只打开已有的库，文件不存在直接报错
    connection = sqlite3.connect(
        f"{config.DB_PATH.as_uri()}?mode=rw", uri=True, factory=Connection
    )
    # 查询结果按列名取值，类似字典
    connection.row_factory = sqlite3.Row
    # 外键约束每个连接都要单独打开
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        # 正常退出提交，抛异常整块回滚
        with connection:
            yield connection
        # 提交成功才执行回调，回滚就丢掉
        for callback in connection.after_commit:
            callback()
    finally:
        connection.close()


# 建表
def create_tables():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(config.DB_PATH)) as connection:
        connection.executescript(SCHEMA)


# 写审计日志
def record_audit(
    connection,
    event,
    *,
    actor_id=None,
    target=None,
    decision=None,
    detail=None,
    thread_id=None,
):
    detail_json = None if detail is None else json.dumps(detail, ensure_ascii=False)
    connection.execute(
        "INSERT INTO audit_log (event, actor_id, target, decision, detail, thread_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event, actor_id, target, decision, detail_json, thread_id),
    )
