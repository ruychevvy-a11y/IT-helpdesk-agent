import logging
from typing import Annotated, Any

from langchain_core.tools import InjectedToolArg, tool

from helpdesk.knowledge import knowledge_base


# 查知识库
@tool(parse_docstring=True)
def search_kb(
    query: str,
    actor: Annotated[Any, InjectedToolArg],
    connection: Annotated[Any, InjectedToolArg],
) -> dict:
    """在 IT 知识库里查「怎么做、怎么排查」，比如 VPN 连不上、打印机脱机、系统权限怎么申请。只会查到当前用户能看的文章，回答时用《标题》注明出处。用户问自己的申请进度、工单、已有权限时，用对应的查询工具，不用这个。

    Args:
        query: 要查的问题，用用户的原话或概括
    """
    try:
        articles = knowledge_base.retrieve(query, actor["role"])
    # 向量库、嵌入或重排出错，告诉模型知识库暂时不可用
    except Exception as error:  # pylint: disable=broad-exception-caught
        logging.exception("知识库检索失败")
        raise ValueError("知识库暂时不可用，可以建议用户转人工") from error
    return {
        "count": len(articles),
        "articles": [
            {"title": article["title"], "text": article["text"]} for article in articles
        ],
    }


# 知识库的工具
KNOWLEDGE_TOOLS = [search_kb]
