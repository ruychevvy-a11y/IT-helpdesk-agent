from helpdesk import config
from seed_db import seed_database


# 重灌库时发件箱一起清空，旧邮件不留
def test_seed_clears_outbox():
    config.OUTBOX_PATH.write_text("E1007\t旧邮件\n", encoding="utf-8")
    seed_database()
    assert not config.OUTBOX_PATH.exists()
