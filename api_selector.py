"""
模型选择器 — 启动时交互选择本地 Ollama 或第三方 API 模型。
"""

import json
import os
import sys
import urllib.request
import urllib.error
from openai import OpenAI


def _base_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


API_CONFIG_PATH = os.path.join(_base_dir(), 'api_config.json')


# ============================================================
# 配置读写
# ============================================================

def _load_json(path: str, default: dict) -> dict:
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default


def _save_json(path: str, data: dict):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


# ============================================================
# 模型拉取
# ============================================================

def _fetch_local_models(base_url: str) -> list:
    """从 Ollama 获取本地模型列表"""
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    root_url = f"{parsed.scheme}://{parsed.netloc}"
    url = root_url + '/api/tags'
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return [m['name'] for m in data.get('models', [])]


def _fetch_api_models(api_key: str, base_url: str) -> list:
    """从第三方 API 获取可用模型列表"""
    client = OpenAI(base_url=base_url, api_key=api_key)
    resp = client.models.list()
    return [m.id for m in resp]


# ============================================================
# 模型展示 & 选择
# ============================================================

def _pick_model(models: list, saved_name: str, label: str) -> str:
    """显示模型列表，让用户选择。返回选中的模型名。"""
    print(f"\n✅ {label}:")
    for i, m in enumerate(models, 1):
        print(f"    [{i}] {m}")

    default_idx = None
    if saved_name and saved_name in models:
        default_idx = models.index(saved_name) + 1
        print(f"\n请选择模型 (1-{len(models)}，直接回车使用上次选择的 {saved_name}):")
    else:
        print(f"\n请选择模型 (1-{len(models)}):")

    while True:
        choice = input("> ").strip()
        if choice == '' and saved_name in models:
            return saved_name
        try:
            idx = int(choice)
            if 1 <= idx <= len(models):
                return models[idx - 1]
        except ValueError:
            pass
        print(f"  请输入 1-{len(models)} 之间的数字")


# ============================================================
# 主入口
# ============================================================

def select_model() -> tuple:
    """
    交互式模型选择主入口。

    Returns:
        (client, model_name) — OpenAI 兼容客户端和模型名称
    """
    import config

    print("\n" + "=" * 60)
    print("  请选择模型来源:")
    print("    [1] 本地 Ollama 模型")
    print("    [2] 第三方 API 模型")
    print("=" * 60)

    while True:
        choice = input("> ").strip()
        if choice in ('1', '2'):
            break
        print("  请输入 1 或 2")

    # ---- 本地模型 ----
    if choice == '1':
        return _select_local(config)

    # ---- 第三方 API ----
    return _select_api(config)


def _select_local(config) -> tuple:
    """本地 Ollama 模型选择流程"""
    cfg = _load_json(API_CONFIG_PATH, {"local_model_name": ""})
    saved = cfg.get('local_model_name', '')

    # 有已保存配置 → 一键确认
    if saved:
        print(f"\n  已保存的本地模型: {saved}")
        print("  直接回车确认，输入 n 重新选择:")
        choice = input("> ").strip()
        if choice == '':
            client = OpenAI(base_url=config.OLLAMA_BASE_URL, api_key='ollama')
            print(f"✅ 已选择本地模型: {saved}")
            return client, saved

    # 拉取本地模型列表
    print("\n⏳ 正在获取本地模型列表...")
    try:
        models = _fetch_local_models(config.OLLAMA_BASE_URL)
    except Exception as e:
        print(f"⚠️  无法连接 Ollama: {e}")
        fallback = saved or config.MODEL_NAME
        print(f"  使用默认模型: {fallback}")
        client = OpenAI(base_url=config.OLLAMA_BASE_URL, api_key='ollama')
        return client, fallback

    if not models:
        print("❌ 未找到本地模型，请确认 Ollama 已安装模型")
        fallback = saved or config.MODEL_NAME
        client = OpenAI(base_url=config.OLLAMA_BASE_URL, api_key='ollama')
        return client, fallback

    model_name = _pick_model(models, saved, "可用本地模型")

    cfg['local_model_name'] = model_name
    _save_json(API_CONFIG_PATH, cfg)

    client = OpenAI(base_url=config.OLLAMA_BASE_URL, api_key='ollama')
    print(f"✅ 已选择本地模型: {model_name}")
    return client, model_name


def _select_api(config) -> tuple:
    """第三方 API 模型选择流程"""
    cfg = _load_json(API_CONFIG_PATH, {
        "api_base_url": "https://api.deepseek.com/v1/",
        "api_key": "",
        "api_model_name": "",
        "local_model_name": "",
    })

    # 有完整已保存配置 → 一键确认
    saved_key = cfg.get('api_key', '')
    saved_model = cfg.get('api_model_name', '')
    if saved_key and saved_model:
        masked = saved_key[:8] + '***'
        print(f"\n  已保存的 API 配置:")
        print(f"    Base URL: {cfg['api_base_url']}")
        print(f"    API Key:  {masked}")
        print(f"    模型:     {saved_model}")
        print("  直接回车确认，输入 n 重新设置:")
        choice = input("> ").strip()
        if choice == '':
            client = OpenAI(base_url=cfg['api_base_url'], api_key=saved_key)
            print(f"✅ 已选择: {saved_model} (API)")
            return client, saved_model

    # 完整配置流程
    # Base URL
    print(f"\n请输入 API Base URL（直接回车使用默认 {cfg['api_base_url']}）:")
    url_input = input("> ").strip()
    if url_input:
        cfg['api_base_url'] = url_input

    # API Key
    if cfg.get('api_key'):
        masked = cfg['api_key'][:8] + '***'
        print(f"检测到已保存的 API Key: {masked}")
        print("直接回车使用已保存的 Key，或输入 new 重新输入:")
        key_input = input("> ").strip()
        if key_input.lower() == 'new':
            cfg['api_key'] = ''

    if not cfg.get('api_key'):
        print("请输入 API Key:")
        cfg['api_key'] = input("> ").strip()
        if not cfg['api_key']:
            print("❌ API Key 不能为空，回退到本地模型")
            return _select_local(config)

    # 拉取模型列表
    print("\n⏳ 正在获取可用模型列表...")
    try:
        models = _fetch_api_models(cfg['api_key'], cfg['api_base_url'])
    except Exception as e:
        print(f"❌ 获取模型列表失败: {e}")
        print("是否重试？(y/n，输入 n 回退到本地模型)")
        if input("> ").strip().lower() == 'y':
            return select_model()
        return _select_local(config)

    if not models:
        print("❌ 未找到可用模型，回退到本地模型")
        return _select_local(config)

    saved = cfg.get('api_model_name', '')
    model_name = _pick_model(models, saved, "可用 API 模型")

    cfg['api_model_name'] = model_name
    _save_json(API_CONFIG_PATH, cfg)

    client = OpenAI(base_url=cfg['api_base_url'], api_key=cfg['api_key'])
    print(f"✅ 已选择: {model_name} (API)")
    return client, model_name
