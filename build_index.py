from helpdesk import config
from helpdesk.knowledge.knowledge_base import index_articles

if __name__ == "__main__":
    print(f"{index_articles()} 篇文章写入 {config.KB_COLLECTION}")
