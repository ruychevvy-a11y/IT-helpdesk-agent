import os
from pathlib import Path

from dotenv import load_dotenv

# =============================================#
#                     路径                     #
# =============================================#

# 项目根目录，所有路径都从这里拼，不管从哪个目录启动都能找到文件
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"

# 业务库，存员工、系统、权限、申请、工单、重置验证码和审计日志
DB_PATH = DATA_DIR / "helpdesk.db"

# 对话线程和审批线程的检查点
CHECKPOINT_DB_PATH = DATA_DIR / "checkpoints.db"

# 种子数据，seed_db.py 从这里灌库
SEED_PATH = DATA_DIR / "seed.json"

# 模拟邮件发件箱，重置验证码只发给员工本人，不经过模型
OUTBOX_PATH = DATA_DIR / "outbox.log"

# 知识库文章，一篇一个 md
KB_DIR = PROJECT_ROOT / "kb"

# 检索评测题
QUERIES_PATH = DATA_DIR / "kb_queries.jsonl"

# 场景评测
SCENARIOS_PATH = DATA_DIR / "scenarios.jsonl"

# 评测结果
EVALUATION_DIR = PROJECT_ROOT / "evaluations"

# =============================================#
#                     模型                     #
# =============================================#
load_dotenv(PROJECT_ROOT / ".env")

DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")

# 百炼的 OpenAI 兼容接口，对话模型走这里；嵌入和重排走 dashscope SDK
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 对话模型
CHAT_MODEL = "qwen3.8-flash"

# 思考模式开关，选工具和填参数不需要长推理
ENABLE_THINKING = False

# 文本嵌入模型和向量维度，建表和检索必须一致
EMBEDDING_MODEL = "qwen3.7-text-embedding"
EMBEDDING_DIM = 1024

# 重排模型
RERANK_MODEL = "qwen3.7-text-rerank"

# =============================================#
#                    知识库                    #
# =============================================#

# 本地 Milvus，复用 hybrid_doc_rag 的部署
MILVUS_URI = "http://localhost:19530"

# 知识库表，一篇文章一行
KB_COLLECTION = "it_helpdesk_kb"
