from functools import cache

import yaml
from langchain_milvus import BM25BuiltInFunction, Milvus
from pymilvus import DataType, Function, FunctionType

from helpdesk import config
from helpdesk.knowledge.dashscope_api import DashScopeEmbeddings, rerank_scores

# =============================================#
#                    读文章                    #
# =============================================#


# 读文章
def load_articles():
    articles = []
    for path in sorted(config.KB_DIR.glob("*.md")):
        _, frontmatter, body = path.read_text(encoding="utf-8").split("---\n", 2)
        articles.append({**yaml.safe_load(frontmatter), "body": body.strip()})
    return articles


# =============================================#
#                     建库                     #
# =============================================#

# 能看文章的角色
ROLES = ("employee", "manager", "it_admin")

# dense 路索引：HNSW 图索引(近似最近邻索引)，余弦距离
DENSE_INDEX_TYPE = "HNSW"
DENSE_METRIC_TYPE = "COSINE"

# HNSW 建图参数：M = 每个节点的出边数，efConstruction = 建图时的候选队列长度
HNSW_PARAMS = {"M": 16, "efConstruction": 200}

# HNSW 搜索候选队列长度：越大越接近精确检索，须 ≥ 每一路的召回数
HNSW_SEARCH_EF = 256

# BM25 分词：jieba 切中文，只留中文、字母和数字，英文转小写
BM25_ANALYZER = {"tokenizer": "jieba", "filter": ["cnalphanumonly", "lowercase"]}

# BM25 超参：k1 控制词频饱和，b 控制长文档惩罚
BM25_PARAMS = {"bm25_k1": 1.2, "bm25_b": 0.75}

# 元数据字段的类型，visible_roles 存成字符串数组才能用 ARRAY_CONTAINS 过滤
METADATA_SCHEMA = {
    "title": {"dtype": DataType.VARCHAR, "kwargs": {"max_length": 128}},
    "visible_roles": {
        "dtype": DataType.ARRAY,
        "kwargs": {
            "element_type": DataType.VARCHAR,
            "max_capacity": len(ROLES),
            "max_length": 16,
        },
    },
}


# 连上知识库表，drop_old 为 True 时删表重建
def vector_store(drop_old=False):
    # dense路
    dense_index = {
        "index_type": DENSE_INDEX_TYPE,
        "metric_type": DENSE_METRIC_TYPE,
        "params": HNSW_PARAMS,
    }
    dense_search = {"params": {"ef": HNSW_SEARCH_EF}}
    # sparse路
    bm25_function = BM25BuiltInFunction(
        input_field_names="text",
        output_field_names="sparse",
        analyzer_params=BM25_ANALYZER,
    )
    sparse_index = {
        "index_type": "SPARSE_INVERTED_INDEX",
        "metric_type": "BM25",
        "params": {"inverted_index_algo": "DAAT_MAXSCORE", **BM25_PARAMS},
    }
    sparse_search = {}
    return Milvus(
        collection_name=config.KB_COLLECTION,
        connection_args={"uri": config.MILVUS_URI},
        drop_old=drop_old,
        primary_field="doc_id",
        auto_id=False,
        text_field="text",
        metadata_schema=METADATA_SCHEMA,
        embedding_function=DashScopeEmbeddings(),
        builtin_function=bm25_function,
        vector_field=["dense", "sparse"],
        index_params=[dense_index, sparse_index],
        search_params=[dense_search, sparse_search],
    )


# 写入库，删表重建，嵌入全部文章写进 Milvus，一篇一行，正文开头加《标题》 --> 写入的篇数
def index_articles():
    articles = load_articles()
    ids = vector_store(drop_old=True).add_texts(
        texts=[f"《{article['title']}》\n{article['body']}" for article in articles],
        metadatas=[
            {"title": article["title"], "visible_roles": article["visible_roles"]}
            for article in articles
        ],
        ids=[article["doc_id"] for article in articles],
    )
    return len(ids)


# =============================================#
#                     检索                     #
# =============================================#

# 融合时每一路取前多少篇
FUSION_DEPTH = 10

# RRF 平滑常数：融合分 = Σ 1/(RRF_K + 名次)
RRF_K = 60

# 融合后取前多少篇交给重排
RERANK_DEPTH = 10

# 每题最终返回多少篇，交给模型作答
RETRIEVE_K = 4


# 检索用的连接，进程内只连一次
@cache
def search_store():
    return vector_store()


# 检索，BM25 和 dense 两路都按角色过滤，RRF 融合 --> 重排 --> 取前 RETRIEVE_K 篇
def retrieve(question, role):
    if role not in ROLES:
        raise ValueError(f"未知角色 {role}")
    # 召回，过滤后发到两路，RRF融合
    search_filter = f'ARRAY_CONTAINS(visible_roles, "{role}")'
    rrf_ranker = Function(
        name="rrf",
        input_field_names=[],
        function_type=FunctionType.RERANK,
        params={"reranker": "rrf", "k": RRF_K},
    )
    candidates = search_store().similarity_search(
        question,
        k=RERANK_DEPTH,
        fetch_k=FUSION_DEPTH,
        expr=search_filter,
        reranker=rrf_ranker,
    )
    if not candidates:
        return []
    # 重排，按重排分从高到低，取前RETRIEVE_K篇
    scores = rerank_scores(question, [document.page_content for document in candidates])
    ranked = sorted(zip(candidates, scores), key=lambda pair: pair[1], reverse=True)
    return [
        {
            "doc_id": document.metadata["doc_id"],
            "title": document.metadata["title"],
            "text": document.page_content,
        }
        for document, score in ranked[:RETRIEVE_K]
    ]
