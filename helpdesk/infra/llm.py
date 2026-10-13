from langchain_openai import ChatOpenAI

from helpdesk import config

# 单次请求超时（秒）
REQUEST_TIMEOUT = 30

# 失败后最多重试几次
MAX_RETRIES = 2

# 采样温度，越靠近0，概率越集中在最高分词，越贪心
# Qwen3 系列的模型卡明确说不要用贪心解码，会让效果下降，甚至无限重复
TEMPERATURE = 0.7


# 创建对话模型
def create_chat_model():
    if not config.DASHSCOPE_API_KEY:
        raise RuntimeError("未读到api_key")
    return ChatOpenAI(
        model=config.CHAT_MODEL,
        base_url=config.DASHSCOPE_BASE_URL,
        api_key=config.DASHSCOPE_API_KEY,
        temperature=TEMPERATURE,
        timeout=REQUEST_TIMEOUT,
        max_retries=MAX_RETRIES,
        # extra_body放厂商独有、OpenAI 标准里没有的参数， 取消思考
        extra_body={"enable_thinking": config.ENABLE_THINKING},
    )
