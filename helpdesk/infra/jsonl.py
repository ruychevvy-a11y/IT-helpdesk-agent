import json


# 读 jsonl --> 每行一个 dict
def load_jsonl(path):
    with open(path, encoding="utf-8") as file:
        return [json.loads(line) for line in file]


# 把一组 dict 写成 jsonl
def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
