import random
import time
from functools import partial

import requests
from dashscope import TextEmbedding, TextReRank
from langchain_core.embeddings import Embeddings

from helpdesk import config

# =============================================#
#                     重试                     #
# =============================================#

# 临时性失败最多尝试几次（含第一次）
API_MAX_ATTEMPTS = 5


# 调一次 dashscope 接口，限流、服务端错误、网络异常按指数退避重试，其余错误直接抛
def call_with_retry(call):
    for attempt in range(API_MAX_ATTEMPTS):
        try:
            response = call()
        # 等回复超时直接抛，不重试
        except requests.exceptions.ReadTimeout:
            raise
        # 连接重置等网络层异常
        except Exception as error:  # pylint: disable=broad-exception-caught
            failure = repr(error)
        else:
            if response.status_code == 200:
                return response
            failure = f"{response.status_code} {response.code} {response.message}"
            transient = (
                response.status_code == 429
                or response.status_code >= 500
                or str(response.code).startswith(("Throttling", "InternalError"))
            )
            if not transient:
                raise RuntimeError(f"dashscope 调用失败：{failure}")
        # 等 1、2、4、8 秒，带随机抖动
        if attempt < API_MAX_ATTEMPTS - 1:
            time.sleep(2**attempt * random.uniform(0.5, 1.0))
    raise RuntimeError(f"dashscope 重试 {API_MAX_ATTEMPTS} 次仍失败：{failure}")


# =============================================#
#                     嵌入                     #
# =============================================#

# 嵌入接口单次最多几条
EMBEDDING_BATCH_SIZE = 20


# 调文本嵌入模型，分批发送，结果按 text_index 排回原顺序；text_type 区分文档和问题
def call_embedding_model(texts, text_type):
    vectors = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        response = call_with_retry(
            partial(
                TextEmbedding.call,
                model=config.EMBEDDING_MODEL,
                input=texts[start : start + EMBEDDING_BATCH_SIZE],
                text_type=text_type,
                dimension=config.EMBEDDING_DIM,
                api_key=config.DASHSCOPE_API_KEY,
            )
        )
        embeddings = sorted(
            response.output["embeddings"], key=lambda item: item["text_index"]
        )
        vectors.extend(item["embedding"] for item in embeddings)
    return vectors


# LangChain 嵌入接口，交给 langchain-milvus 用
class DashScopeEmbeddings(Embeddings):

    def embed_documents(self, texts):
        return call_embedding_model(texts, "document")

    def embed_query(self, text):
        return call_embedding_model([text], "query")[0]


# =============================================#
#                     重排                     #
# =============================================#

# 重排的任务说明
RERANK_INSTRUCT = (
    "Given an IT helpdesk question, "
    "judge whether the knowledge base article answers it."
)


# 重排模型给每段文本打相关分 --> 与 texts 同序的分数
def rerank_scores(question, texts):
    response = call_with_retry(
        partial(
            TextReRank.call,
            model=config.RERANK_MODEL,
            query=question,
            documents=texts,
            instruct=RERANK_INSTRUCT,
            api_key=config.DASHSCOPE_API_KEY,
        )
    )
    scores = [0.0] * len(texts)
    for result in response.output["results"]:
        scores[result["index"]] = result["relevance_score"]
    return scores
