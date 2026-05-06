"""
AI 回复模块 — 通过 Ollama 调用本地 DeepSeek 模型
"""

import os
import sys
import re
from openai import OpenAI


def _base_dir() -> str:
    """程序所在目录（兼容 PyInstaller 打包和源码运行）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# 从配置读取
try:
    import config
    BASE_URL = config.OLLAMA_BASE_URL
    MODEL = config.MODEL_NAME
    TEMPERATURE = config.TEMPERATURE
    MAX_TOKENS = config.MAX_TOKENS
    BASE_SYSTEM_PROMPT = config.SYSTEM_PROMPT
    MAX_HISTORY_ROUNDS = config.MAX_HISTORY_ROUNDS
    SKILLS_DIR = getattr(config, 'SKILLS_DIR', 'skills')
except ImportError:
    BASE_URL = 'http://localhost:11434/v1/'
    MODEL = 'deepseek-r1:7b'
    TEMPERATURE = 0.7
    MAX_TOKENS = 300
    BASE_SYSTEM_PROMPT = (
        "你是一个微信自动回复助手，请用自然口语化的中文回复。"
        "回复要简短，1-3句话即可，不要说自己是AI。"
    )
    MAX_HISTORY_ROUNDS = 15
    SKILLS_DIR = 'skills'


def _load_skills() -> str:
    """加载 skills 目录下的所有 skill 文件，追加到系统提示词中。"""
    skills_path = os.path.join(_base_dir(), SKILLS_DIR)
    if not os.path.isdir(skills_path):
        return BASE_SYSTEM_PROMPT

    skill_files = []
    for fname in os.listdir(skills_path):
        if fname.endswith(('.txt', '.md', '.skill')):
            skill_files.append(fname)
    if not skill_files:
        return BASE_SYSTEM_PROMPT

    skill_files.sort()
    parts = [BASE_SYSTEM_PROMPT, '', '--- 自定义 Skill ---']
    for fname in skill_files:
        fpath = os.path.join(skills_path, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if content:
                parts.append(f'[{fname}]\n{content}')
        except Exception as e:
            print(f"⚠️  读取 skill 文件失败 {fname}: {e}")

    if len(parts) <= 3:
        return BASE_SYSTEM_PROMPT
    return '\n\n'.join(parts)


SYSTEM_PROMPT = _load_skills()

# 会话内存: {user_name: [{"role": ..., "content": ...}, ...]}
message_table: dict = {}

# 动态客户端 — 由 app.py 在启动时通过 init_client() 注入
client = None
MODEL = None


def init_client(_client, model_name: str):
    """由 app.py 在模型选择完成后调用，注入 OpenAI 兼容客户端和模型名。"""
    global client, MODEL
    client = _client
    MODEL = model_name


def reload_skills():
    """重新加载 skill 文件（无需重启程序即可热更新）"""
    global SYSTEM_PROMPT
    SYSTEM_PROMPT = _load_skills()
    print(f"✅ Skill 已重新加载")


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
