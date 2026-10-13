import json

from helpdesk import config
from helpdesk.infra.jsonl import load_jsonl, write_jsonl
from helpdesk.knowledge.knowledge_base import load_articles, retrieve


def main():
    queries = load_jsonl(config.QUERIES_PATH)
    visible_roles = {
        article["doc_id"]: article["visible_roles"] for article in load_articles()
    }
    # 逐题检索，记下命中、跨篇覆盖和越权泄漏
    run = []
    for query in queries:
        doc_ids = [
            article["doc_id"] for article in retrieve(query["question"], query["role"])
        ]
        gold = set(query["gold_doc_ids"])
        run.append(
            {
                "query_id": query["query_id"],
                "type": query["type"],
                "gold_doc_ids": query["gold_doc_ids"],
                "doc_ids": doc_ids,
                "hit": bool(gold & set(doc_ids)),
                "coverage": gold <= set(doc_ids),
                "leaked_doc_ids": [
                    doc_id
                    for doc_id in doc_ids
                    if query["role"] not in visible_roles[doc_id]
                    or doc_id in query["forbidden_doc_ids"]
                ],
            }
        )

    # 汇总，命中只算有金标的题，跨篇覆盖按金标篇数分开算，泄漏算全部题
    gold_rows = [row for row in run if row["type"] != "forbidden"]
    results = {
        "num_queries": len(gold_rows),
        "hits": sum(row["hit"] for row in gold_rows),
        "leaks": sum(len(row["leaked_doc_ids"]) for row in run),
    }
    for num_gold in (2, 3):
        cross_rows = [
            row
            for row in run
            if row["type"] == "cross_doc" and len(row["gold_doc_ids"]) == num_gold
        ]
        results[f"cross_{num_gold}_docs"] = {
            "num_queries": len(cross_rows),
            "covered": sum(row["coverage"] for row in cross_rows),
        }

    # 写逐题结果和汇总
    config.EVALUATION_DIR.mkdir(exist_ok=True)
    write_jsonl(config.EVALUATION_DIR / "run_retrieval.jsonl", run)
    with open(
        config.EVALUATION_DIR / "results_retrieval.json", "w", encoding="utf-8"
    ) as file:
        json.dump(results, file, ensure_ascii=False, indent=2)
    cross_two, cross_three = results["cross_2_docs"], results["cross_3_docs"]
    print(
        f"命中 {results['hits']}/{results['num_queries']}，"
        f"跨两篇全覆盖 {cross_two['covered']}/{cross_two['num_queries']}，"
        f"跨三篇全覆盖 {cross_three['covered']}/{cross_three['num_queries']}，"
        f"越权泄漏 {results['leaks']} 次"
    )
    if results["leaks"]:
        raise AssertionError("有越权泄漏，见 run_retrieval.jsonl 的 leaked_doc_ids")


if __name__ == "__main__":
    main()
