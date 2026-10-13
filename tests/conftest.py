import shutil

import pytest

from helpdesk import config
from helpdesk.infra import db
from seed_db import seed_database


# 整轮测试只灌一次种子库，当模板
@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory):
    template_dir = tmp_path_factory.mktemp("template")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(config, "DB_PATH", template_dir / "helpdesk.db")
        monkeypatch.setattr(
            config, "CHECKPOINT_DB_PATH", template_dir / "checkpoints.db"
        )
        monkeypatch.setattr(config, "OUTBOX_PATH", template_dir / "outbox.log")
        seed_database()
    return template_dir / "helpdesk.db"


# 每个测试一份全新的库副本，自动生效，碰不到真实的库
@pytest.fixture(autouse=True)
def fresh_database(seeded_template, tmp_path, monkeypatch):
    database_path = tmp_path / "helpdesk.db"
    shutil.copyfile(seeded_template, database_path)
    monkeypatch.setattr(config, "DB_PATH", database_path)
    monkeypatch.setattr(config, "CHECKPOINT_DB_PATH", tmp_path / "checkpoints.db")
    monkeypatch.setattr(config, "OUTBOX_PATH", tmp_path / "outbox.log")


# 以某个员工的身份调用工具，替网关查 actor、开事务、注入参数
@pytest.fixture
def call_tool():
    def call(tool, actor_id, **args):
        with db.transaction() as connection:
            actor = connection.execute(
                "SELECT id, role, department FROM employees WHERE id = ?", (actor_id,)
            ).fetchone()
            return tool.invoke({**args, "actor": actor, "connection": connection})

    return call
