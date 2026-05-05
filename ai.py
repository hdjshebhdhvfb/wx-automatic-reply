"""
AI 回复模块 — 通过 Ollama 调用本地 DeepSeek 模型
"""

import re
from openai import OpenAI

# 从配置读取
try:
    import config
    BASE_URL = config.OLLAMA_BASE_URL
    MODEL = config.MODEL_NAME
    TEMPERATURE = config.TEMPERATURE
    MAX_TOKENS = config.MAX_TOKENS
    SYSTEM_PROMPT = config.SYSTEM_PROMPT
    MAX_HISTORY_ROUNDS = config.MAX_HISTORY_ROUNDS
except ImportError:
    # 配置文件不存在时的默认值
    BASE_URL = 'http://localhost:11434/v1/'
    MODEL = 'deepseek-r1:7b'
    TEMPERATURE = 0.7
    MAX_TOKENS = 300
    SYSTEM_PROMPT = (
        "你是一个微信自动回复助手，请用自然口语化的中文回复。"
        "回复要简短，1-3句话即可，不要说自己是AI。"
    )
    MAX_HISTORY_ROUNDS = 15

# OpenAI 兼容客户端（连接本地 Ollama）
client = OpenAI(base_url=BASE_URL, api_key='ollama')

# 会话内存: {user_name: [{"role": ..., "content": ...}, ...]}
message_table: dict = {}


def add_user(user_name: str):
    """注册用户"""
    if user_name not in message_table:
        message_table[user_name] = []


def _clean(content: str) -> str:
    """清理 AI 输出中的 markdown / 思考标签"""
    # 去掉 <think>...</think>
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
    # 去掉 markdown 标记
    content = re.sub(r'\[([^\]]*)\]\([^\)]*\)', r'\1', content)  # 链接 → 只保留文字
    content = re.sub(r'\*\*\*|\*\*|\*', '', content)
    content = re.sub(r'`{1,3}[^`]*`{1,3}', '', content)
    content = re.sub(r'^[#\-*>]+\s*', '', content, flags=re.MULTILINE)
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content.strip()


def chat(user_name: str, prompt: str) -> str:
    """
    调用 AI 生成回复。

    Args:
        user_name: 好友名称
        prompt: 对方发送的消息

    Returns:
        AI 生成的回复文本
    """
    if user_name not in message_table:
        add_user(user_name)

    # 记录用户消息
    message_table[user_name].append({"role": "user", "content": prompt})

    try:
        # 构建消息列表：系统提示 + 最近的对话历史
        max_msgs = MAX_HISTORY_ROUNDS * 2  # 每轮=2条(user+assistant)
        recent = message_table[user_name][-max_msgs:]
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + recent

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )

        content = response.choices[0].message.content
        content = _clean(content)

        if not content:
            content = "好的，我知道了。"

        # 记录 AI 回复
        message_table[user_name].append({"role": "assistant", "content": content})

        return content

    except Exception as e:
        print(f"❌ AI 调用失败: {e}")
        return "抱歉，我现在有点事，稍后回复你。"


def clear_history(user_name: str):
    """清除指定用户的会话历史"""
    if user_name in message_table:
        message_table[user_name].clear()
