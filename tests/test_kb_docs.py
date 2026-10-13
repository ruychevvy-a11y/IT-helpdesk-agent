import json
import re

import chat
from helpdesk import config
from helpdesk.iam import auth
from helpdesk.infra.jsonl import load_jsonl
from helpdesk.knowledge import knowledge_base

# KCS 文章的小节，按这个顺序出现
SECTIONS = ["问题", "环境", "解决步骤", "原因", "相关"]

# 必须有的小节，原因可选
REQUIRED_SECTIONS = {"问题", "环境", "解决步骤", "相关"}

ARTICLES = knowledge_base.load_articles()
ARTICLES_BY_TITLE = {article["title"]: article for article in ARTICLES}
ARTICLES_BY_ID = {article["doc_id"]: article for article in ARTICLES}
ALL_TEXT = "\n".join(article["body"] for article in ARTICLES)
QUERIES = load_jsonl(config.QUERIES_PATH)


# 文章正文按 ## 拆成 {小节名: 内容}
def article_sections(article):
    names = re.findall(r"^## (.+)$", article["body"], re.M)
    contents = re.split(r"^## .+$", article["body"], flags=re.M)[1:]
    return dict(zip(names, contents))


# 小节按 KCS 顺序，问题写两三种说法，步骤从 1 连续编号
def test_articles_follow_kcs_sections():
    assert len(ARTICLES_BY_TITLE) == len(ARTICLES)
    for article in ARTICLES:
        sections = article_sections(article)
        names = list(sections)
        assert names == [name for name in SECTIONS if name in sections], article[
            "doc_id"
        ]
        assert REQUIRED_SECTIONS <= set(names), article["doc_id"]
        phrasings = re.findall(r"^- ", sections["问题"], re.M)
        assert 2 <= len(phrasings) <= 3, article["doc_id"]
        steps = [
            int(number)
            for number in re.findall(r"^(\d+)\. ", sections["解决步骤"], re.M)
        ]
        assert steps == list(range(1, len(steps) + 1)), article["doc_id"]


# 《》引用的文章都存在，而且能看引用方的人也能看被引用的文章
def test_related_articles_exist_and_are_visible():
    for article in ARTICLES:
        for title in re.findall(r"《(.+?)》", article["body"]):
            assert title in ARTICLES_BY_TITLE, (article["doc_id"], title)
            referenced_roles = set(ARTICLES_BY_TITLE[title]["visible_roles"])
            assert set(article["visible_roles"]) <= referenced_roles, (
                article["doc_id"],
                title,
            )


# 正文不出现具体人名，只写角色
def test_articles_mention_no_employee_names():
    seed = json.loads(config.SEED_PATH.read_text(encoding="utf-8"))
    for employee in seed["employees"]:
        assert employee["name"] not in ALL_TEXT, employee["name"]


# 文章里的锁定、密码、验证码规则和代码常量一致
def test_account_rules_match_code():
    for fact in [
        f"连续输错密码 {auth.MAX_FAILED_LOGINS} 次，账号锁定 {auth.LOCKOUT_MINUTES} 分钟",
        f"至少 {auth.MIN_PASSWORD_LENGTH} 位",
        f"验证码 {auth.RESET_CODE_MINUTES} 分钟内有效，输错 {auth.MAX_RESET_CODE_ATTEMPTS} 次需要重新获取",
        f"{auth.RESET_CODE_COOLDOWN_SECONDS} 秒后可以再发一次",
    ]:
        assert fact in ALL_TEXT, fact


# 文章提到的命令都真实存在，系统名和生产服务器有效期和种子一致
def test_commands_and_systems_match_code():
    commands = set(chat.COMMANDS) | {"/forgot"}
    for command in re.findall(r"(?<![\w:/.~])/[a-z_]+", ALL_TEXT):
        assert command in commands, command
    seed = json.loads(config.SEED_PATH.read_text(encoding="utf-8"))
    levels = ARTICLES_BY_ID["access_levels"]["body"]
    for system in seed["systems"]:
        assert system["name"].replace("（堡垒机）", "") in levels, system["name"]
        if system["grant_days"]:
            assert f"{system['grant_days']} 天自动失效" in levels


# 评测题的金标看得见、越权题的目标看不见，关键事实能在金标文章里找到
def test_queries_reference_valid_articles():
    for query in QUERIES:
        for doc_id in query["gold_doc_ids"]:
            assert query["role"] in ARTICLES_BY_ID[doc_id]["visible_roles"], query
        for doc_id in query["forbidden_doc_ids"]:
            assert query["role"] not in ARTICLES_BY_ID[doc_id]["visible_roles"], query
        gold_text = "".join(
            ARTICLES_BY_ID[doc_id]["body"] for doc_id in query["gold_doc_ids"]
        )
        for fact in query["key_facts"]:
            assert fact in gold_text, (query["query_id"], fact)
        expected_gold = {"single": (1,), "cross_doc": (2, 3), "forbidden": (0,)}
        assert len(query["gold_doc_ids"]) in expected_gold[query["type"]], query[
            "query_id"
        ]


# 每篇受限文章都有一道越权题
def test_every_restricted_article_has_forbidden_query():
    restricted = {
        article["doc_id"]
        for article in ARTICLES
        if set(article["visible_roles"]) != set(knowledge_base.ROLES)
    }
    covered = {doc_id for query in QUERIES for doc_id in query["forbidden_doc_ids"]}
    assert restricted == covered
