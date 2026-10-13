import pytest
from langchain_core.documents import Document

from helpdesk import config
from helpdesk.knowledge import knowledge_base
from helpdesk.tools import knowledge


# 替身向量库，记下检索参数，按给定顺序返回候选
class FakeStore:
    def __init__(self, documents):
        self.documents = documents
        self.search_kwargs = None

    def similarity_search(self, query, **kwargs):
        self.search_kwargs = {"query": query, **kwargs}
        return self.documents


# 一篇候选文章
def candidate(doc_id):
    return Document(
        page_content=f"《{doc_id}》\n正文",
        metadata={
            "doc_id": doc_id,
            "title": doc_id,
            "visible_roles": ["employee", "manager", "it_admin"],
        },
    )


# 用替身向量库和替身重排跑一次检索 --> (结果, 替身向量库, 每次送去重排的文本)
def run_retrieve(monkeypatch, documents, scores, role="employee"):
    store = FakeStore(documents)
    reranked_texts = []

    def fake_rerank_scores(question, texts):
        reranked_texts.append(texts)
        return scores

    monkeypatch.setattr(knowledge_base, "search_store", lambda: store)
    monkeypatch.setattr(knowledge_base, "rerank_scores", fake_rerank_scores)
    results = knowledge_base.retrieve("VPN 连不上", role)
    return results, store, reranked_texts


# 每篇文章都拆出 frontmatter 和正文，doc_id 和文件名一致
def test_load_articles_parses_frontmatter():
    articles = knowledge_base.load_articles()
    assert len(articles) == len(list(config.KB_DIR.glob("*.md")))
    for article in articles:
        assert set(article) == {"doc_id", "title", "visible_roles", "category", "body"}
        assert (config.KB_DIR / f"{article['doc_id']}.md").exists()
        assert set(article["visible_roles"]) <= set(knowledge_base.ROLES)
        assert "it_admin" in article["visible_roles"]
        assert article["body"].startswith("## 问题")


# 不认识的角色直接报错，不去碰向量库
def test_retrieve_rejects_unknown_role(monkeypatch):
    def unexpected_store():
        raise AssertionError("角色不对时不应该连向量库")

    monkeypatch.setattr(knowledge_base, "search_store", unexpected_store)
    with pytest.raises(ValueError, match="未知角色"):
        knowledge_base.retrieve("VPN 连不上", "admin")


# 过滤条件带上当前角色，每一路召回数、融合方式按常量传
def test_retrieve_passes_role_filter_and_depths(monkeypatch):
    _, store, _ = run_retrieve(
        monkeypatch, [candidate("vpn_connect")], [0.9], role="manager"
    )
    assert store.search_kwargs["query"] == "VPN 连不上"
    assert store.search_kwargs["expr"] == 'ARRAY_CONTAINS(visible_roles, "manager")'
    assert store.search_kwargs["fetch_k"] == knowledge_base.FUSION_DEPTH
    assert store.search_kwargs["k"] == knowledge_base.RERANK_DEPTH
    assert store.search_kwargs["reranker"].params == {
        "reranker": "rrf",
        "k": knowledge_base.RRF_K,
    }


# 按重排分从高到低取前 RETRIEVE_K 篇，只返回 doc_id、标题和正文
def test_retrieve_orders_by_rerank_scores(monkeypatch):
    documents = [candidate(doc_id) for doc_id in ("a", "b", "c", "d", "e")]
    results, _, reranked_texts = run_retrieve(
        monkeypatch, documents, [0.1, 0.9, 0.5, 0.3, 0.7]
    )
    assert [result["doc_id"] for result in results] == ["b", "e", "c", "d"]
    assert len(results) == knowledge_base.RETRIEVE_K
    assert set(results[0]) == {"doc_id", "title", "text"}
    assert reranked_texts == [[document.page_content for document in documents]]


# 过滤后没有候选，直接返回空列表，不调重排
def test_retrieve_without_candidates_skips_rerank(monkeypatch):
    results, _, reranked_texts = run_retrieve(monkeypatch, [], [])
    assert results == []
    assert reranked_texts == []


# 建库删表重建，一篇一行，正文前加《标题》，元数据只有标题和可见角色
def test_index_articles_writes_one_row_per_article(monkeypatch):
    calls = {}

    class FakeIndexStore:
        def add_texts(self, texts, metadatas, ids):
            calls.update(texts=texts, metadatas=metadatas, ids=ids)
            return ids

    def fake_vector_store(drop_old=False):
        calls["drop_old"] = drop_old
        return FakeIndexStore()

    monkeypatch.setattr(knowledge_base, "vector_store", fake_vector_store)
    articles = knowledge_base.load_articles()
    assert knowledge_base.index_articles() == len(articles)
    assert calls["drop_old"] is True
    assert calls["ids"] == [article["doc_id"] for article in articles]
    for article, text, metadata in zip(articles, calls["texts"], calls["metadatas"]):
        assert text == f"《{article['title']}》\n{article['body']}"
        assert metadata == {
            "title": article["title"],
            "visible_roles": article["visible_roles"],
        }


# 替身检索，记下用什么角色查的
def fake_retrieve(calls):
    def retrieve(question, role):
        calls.append((question, role))
        return [
            {
                "doc_id": "vpn_connect",
                "title": "VPN 连不上",
                "text": "《VPN 连不上》正文",
            }
        ]

    return retrieve


# 模型能填的参数只有 query，角色看不到
def test_search_kb_hides_role_from_model():
    schema = knowledge.search_kb.tool_call_schema.model_json_schema()
    assert list(schema["properties"]) == ["query"]


# 角色取自网关注入的 actor，返回标题和正文
def test_search_kb_uses_actor_role(call_tool, monkeypatch):
    calls = []
    monkeypatch.setattr(knowledge_base, "retrieve", fake_retrieve(calls))
    employee_result = call_tool(knowledge.search_kb, "E1007", query="VPN 连不上")
    call_tool(knowledge.search_kb, "E1003", query="VPN 连不上")
    assert calls == [("VPN 连不上", "employee"), ("VPN 连不上", "it_admin")]
    assert employee_result == {
        "count": 1,
        "articles": [{"title": "VPN 连不上", "text": "《VPN 连不上》正文"}],
    }


# 检索出任何错都转成 ValueError，由网关回给模型
def test_search_kb_turns_failures_into_value_error(call_tool, monkeypatch):
    def broken_retrieve(question, role):
        raise RuntimeError("Milvus 连不上")

    monkeypatch.setattr(knowledge_base, "retrieve", broken_retrieve)
    with pytest.raises(ValueError, match="知识库暂时不可用"):
        call_tool(knowledge.search_kb, "E1007", query="VPN 连不上")
