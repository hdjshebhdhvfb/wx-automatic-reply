"""
AI 回复模块 — 支持本地 Ollama 模型和云端 API 两种模式
"""

import re
from openai import OpenAI

try:
    import config
    _BASE_URL = config.OLLAMA_BASE_URL
    _MODEL = config.MODEL_NAME
    _TEMPERATURE = config.TEMPERATURE
    _MAX_TOKENS = config.MAX_TOKENS
    _SYSTEM_PROMPT = config.SYSTEM_PROMPT
    _MAX_HISTORY_ROUNDS = config.MAX_HISTORY_ROUNDS
    _API_KEY = config.API_KEY
    _API_BASE_URL = config.API_BASE_URL
    _API_MODEL_NAME = config.API_MODEL_NAME
except ImportError:
    _BASE_URL = 'http://localhost:11434/v1/'
    _MODEL = 'deepseek-r1:7b'
    _TEMPERATURE = 0.7
    _MAX_TOKENS = 300
    _SYSTEM_PROMPT = (
        "你是一个微信自动回复助手，请用自然口语化的中文回复。"
        "回复要简短，1-3句话即可，不要说自己是AI。"
    )
    _MAX_HISTORY_ROUNDS = 15
    _API_KEY = ''
    _API_BASE_URL = 'https://api.deepseek.com/v1/'
    _API_MODEL_NAME = 'deepseek-chat'

# 会话内存: {user_name: [{"role": ..., "content": ...}, ...]}
message_table: dict = {}

# 当前模式: 'local' 或 'api'
_mode = 'local'

# 本地 Ollama 客户端
_local_client = OpenAI(base_url=_BASE_URL, api_key='ollama')

# 云端 API 客户端（延迟初始化）
_api_client = None

# 当前使用的模型信息（供外部显示）
_current_model_display = ''


def set_mode(mode: str, api_key: str = None, api_base_url: str = None, api_model: str = None):
    """
    切换 AI 模式。

    Args:
        mode: 'local' 或 'api'
        api_key: API Key（仅 api 模式需要，默认从 config 读取）
        api_base_url: API 地址（仅 api 模式需要）
        api_model: 云端模型名（仅 api 模式需要）
    """
    global _mode, _api_client, _current_model_display

    if mode == 'api':
        key = api_key or _API_KEY
        base = api_base_url or _API_BASE_URL
        model = api_model or _API_MODEL_NAME

        if not key:
            raise ValueError("API 模式需要提供 API Key，请在 config.py 中设置 API_KEY")

        _api_client = OpenAI(base_url=base, api_key=key)
        _mode = 'api'
        _current_model_display = f"{model} (云端API)"

    else:
        _mode = 'local'
        _current_model_display = f"{_MODEL} (本地Ollama)"


def get_mode() -> str:
    """返回当前模式: 'local' 或 'api'"""
    return _mode


def get_model_display() -> str:
    """返回当前模型的可读描述，供 UI 显示"""
    if not _current_model_display:
        if _mode == 'api':
            _api_model = _API_MODEL_NAME
            return f"{_api_model} (云端API)"
        return f"{_MODEL} (本地Ollama)"
    return _current_model_display


def add_user(user_name: str):
    """注册用户"""
    if user_name not in message_table:
        message_table[user_name] = []


def _clean(content: str) -> str:
    """清理 AI 输出中的 markdown / 思考标签"""
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
    content = re.sub(r'\[([^\]]*)\]\([^\)]*\)', r'\1', content)
    content = re.sub(r'\*\*\*|\*\*|\*', '', content)
    content = re.sub(r'`{1,3}[^`]*`{1,3}', '', content)
    content = re.sub(r'^[#\-*>]+\s*', '', content, flags=re.MULTILINE)
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content.strip()


def _get_client():
    """根据当前模式返回对应的客户端"""
    if _mode == 'api' and _api_client is not None:
        return _api_client, _API_MODEL_NAME
    return _local_client, _MODEL


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

    message_table[user_name].append({"role": "user", "content": prompt})

    client, model = _get_client()

    try:
        max_msgs = _MAX_HISTORY_ROUNDS * 2
        recent = message_table[user_name][-max_msgs:]
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + recent

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=_TEMPERATURE,
            max_tokens=_MAX_TOKENS,
        )

        content = response.choices[0].message.content
        content = _clean(content)

        if not content:
            content = "好的，我知道了。"

        message_table[user_name].append({"role": "assistant", "content": content})

        return content

    except Exception as e:
        print(f"❌ AI 调用失败: {e}")
        return "抱歉，我现在有点事，稍后回复你。"


def clear_history(user_name: str):
    """清除指定用户的会话历史"""
    if user_name in message_table:
        message_table[user_name].clear()
