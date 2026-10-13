from helpdesk import config


# 模拟发邮件，追加一行到发件箱
def send_email(employee_id, text):
    with config.OUTBOX_PATH.open("a", encoding="utf-8") as outbox:
        outbox.write(f"{employee_id}\t{text}\n")
