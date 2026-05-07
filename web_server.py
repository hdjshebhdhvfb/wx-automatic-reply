"""
微信自动回复系统 Web 控制台
启动: python web_server.py
访问: http://localhost:8000
"""

import json
import os
import sys
import queue
import asyncio
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

import config
import db
import ai
from bot_manager import bot_manager
from api_selector import (
    _fetch_local_models,
    _fetch_api_models,
    _load_json,
    _save_json,
    API_CONFIG_PATH,
)

app = FastAPI(title="微信自动回复控制台")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# 页面
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(BASE_DIR, "ui", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# 状态 & 控制
# ============================================================

@app.get("/api/status")
async def get_status():
    return bot_manager.status


@app.post("/api/start")
async def start_bot(req: Request):
    body = await req.json()
    listeners = body.get("listeners", [])
    use_sse = body.get("use_sse", config.WEFLOW_SSE_ENABLED)
    ok = bot_manager.start(listeners, use_sse)
    return {"ok": ok, "state": bot_manager.state}


@app.post("/api/stop")
async def stop_bot():
    bot_manager.stop()
    return {"ok": True}


# ============================================================
# 实时日志 SSE
# ============================================================

@app.get("/api/logs/stream")
async def log_stream():
    """SSE 实时日志推流，先回放历史再推新日志。"""

    async def generate():
        # 回放历史日志
        for line in bot_manager._log_history:
            yield f"data: {json.dumps({'type': 'log', 'msg': line})}\n\n"

        # 推送新日志
        while True:
            try:
                msg = bot_manager.log_queue.get_nowait()
                yield f"data: {json.dumps({'type': 'log', 'msg': msg})}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(generate(), media_type="text/event-stream")


# ============================================================
# 配置
# ============================================================

@app.get("/api/config")
async def get_config():
    cfg = _load_json(API_CONFIG_PATH, {
        "api_base_url": "https://api.deepseek.com/v1/",
        "api_key": "",
        "api_model_name": "",
        "local_model_name": "",
    })
    # 脱敏 API Key
    api_cfg = dict(cfg)
    if api_cfg.get("api_key"):
        api_cfg["api_key_masked"] = api_cfg["api_key"][:8] + "***"

    return {
        "local_model": cfg.get("local_model_name", config.MODEL_NAME),
        "api_config": api_cfg,
        "current_model": ai.MODEL or cfg.get("local_model_name", config.MODEL_NAME),
        "ollama_url": config.OLLAMA_BASE_URL,
        "temperature": config.TEMPERATURE,
        "max_tokens": config.MAX_TOKENS,
        "reply_delay": config.REPLY_DELAY,
        "message_merge_delay": config.MESSAGE_MERGE_DELAY,
        "listen_mode": config.LISTEN_MODE,
        "max_history_rounds": config.MAX_HISTORY_ROUNDS,
        "use_sse": config.WEFLOW_SSE_ENABLED,
    }


# ============================================================
# 模型
# ============================================================

@app.get("/api/models/local")
async def get_local_models():
    try:
        models = _fetch_local_models(config.OLLAMA_BASE_URL)
        return {"ok": True, "models": models}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/models/api/fetch")
async def fetch_api_models(req: Request):
    body = await req.json()
    api_key = body.get("api_key", "")
    base_url = body.get("base_url", "https://api.deepseek.com/v1/")
    try:
        models = _fetch_api_models(api_key, base_url)
        return {"ok": True, "models": models}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/models/select")
async def select_model(req: Request):
    """保存模型选择并初始化 AI 客户端。"""
    body = await req.json()
    source = body.get("source", "local")

    if source == "local":
        model_name = body.get("model_name", config.MODEL_NAME)
        cfg = _load_json(API_CONFIG_PATH, {})
        cfg["local_model_name"] = model_name
        _save_json(API_CONFIG_PATH, cfg)
        from openai import OpenAI
        client = OpenAI(base_url=config.OLLAMA_BASE_URL, api_key="ollama")
        ai.init_client(client, model_name)
        return {"ok": True, "source": "local", "model": model_name}

    # API
    api_key = body.get("api_key", "")
    base_url = body.get("base_url", "https://api.deepseek.com/v1/")
    model_name = body.get("model_name", "")
    if not api_key or not model_name:
        return {"ok": False, "error": "API Key 和模型名不能为空"}

    cfg = _load_json(API_CONFIG_PATH, {})
    cfg["api_key"] = api_key
    cfg["api_base_url"] = base_url
    cfg["api_model_name"] = model_name
    _save_json(API_CONFIG_PATH, cfg)

    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key)
    ai.init_client(client, model_name)
    return {"ok": True, "source": "api", "model": model_name}


# ============================================================
# 好友列表
# ============================================================

def _names_path() -> str:
    return os.path.join(BASE_DIR, config.NAMES_FILE)


@app.get("/api/friends")
async def get_friends():
    path = _names_path()
    if not os.path.exists(path):
        return {"ok": True, "friends": []}
    with open(path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    return {"ok": True, "friends": lines}


@app.post("/api/friends")
async def save_friends(req: Request):
    body = await req.json()
    friends = body.get("friends", [])
    path = _names_path()
    with open(path, "w", encoding="utf-8") as f:
        for name in friends:
            f.write(name + "\n")
    return {"ok": True, "count": len(friends)}


# ============================================================
# 聊天记录
# ============================================================

@app.get("/api/history/{user_id}")
async def get_history(user_id: str, limit: int = 50):
    records = db.get_history(user_id, limit)
    return {"ok": True, "user_id": user_id, "records": records, "count": len(records)}


# ============================================================
# 好友列表 (聊天记录页用)
# ============================================================

@app.get("/api/history/users")
async def get_history_users():
    """返回有聊天记录的用户列表"""
    import sqlite3
    try:
        conn = sqlite3.connect(db.DB_PATH)
        c = conn.cursor()
        c.execute("SELECT DISTINCT user_id FROM history ORDER BY user_id")
        users = [row[0] for row in c.fetchall()]
        conn.close()
        return {"ok": True, "users": users}
    except Exception as e:
        return {"ok": False, "users": [], "error": str(e)}


# ============================================================
# Skill 管理
# ============================================================

def _skills_dir() -> str:
    return os.path.join(BASE_DIR, getattr(config, 'SKILLS_DIR', 'skills'))


@app.get("/api/skills")
async def get_skills():
    """获取所有 skill 文件列表及内容"""
    sdir = _skills_dir()
    if not os.path.isdir(sdir):
        return {"ok": True, "skills": []}

    skip = {'example-math-tutor.skill'}
    skills = []
    for fname in sorted(os.listdir(sdir)):
        if fname in skip:
            continue
        if fname.endswith(('.txt', '.md', '.skill')):
            fpath = os.path.join(sdir, fname)
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read()
            skills.append({"name": fname, "content": content})
    return {"ok": True, "skills": skills}


@app.post("/api/skills/{name}")
async def save_skill(name: str, req: Request):
    """创建或更新一个 skill 文件"""
    body = await req.json()
    content = body.get("content", "")
    sdir = _skills_dir()
    os.makedirs(sdir, exist_ok=True)

    # 确保文件名有合法后缀
    if not name.endswith(('.txt', '.md', '.skill')):
        name = name + '.md'

    fpath = os.path.join(sdir, name)
    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(content)

    return {"ok": True, "name": name}


@app.delete("/api/skills/{name}")
async def delete_skill(name: str):
    """删除一个 skill 文件"""
    sdir = _skills_dir()
    fpath = os.path.join(sdir, name)
    if os.path.exists(fpath):
        os.remove(fpath)
        return {"ok": True}
    return {"ok": False, "error": "文件不存在"}


@app.post("/api/skills/reload")
async def reload_skills_api():
    """重新加载所有 skill 到 AI 系统提示词"""
    ai.reload_skills()
    return {"ok": True}


# ============================================================
# 启动时自动初始化模型
# ============================================================

def auto_init_model():
    """启动时自动加载已保存的模型配置，初始化 AI 客户端。"""
    cfg = _load_json(API_CONFIG_PATH, {
        "api_base_url": "https://api.deepseek.com/v1/",
        "api_key": "",
        "api_model_name": "",
        "local_model_name": "",
    })

    from openai import OpenAI

    # 优先尝试 API 配置
    if cfg.get("api_key") and cfg.get("api_model_name"):
        try:
            client = OpenAI(base_url=cfg["api_base_url"], api_key=cfg["api_key"])
            ai.init_client(client, cfg["api_model_name"])
            print(f"[OK] Auto-loaded API model: {cfg['api_model_name']}")
            return
        except Exception as e:
            print(f"[WARN] API model load failed: {e}")

    # 回退到本地模型
    model_name = cfg.get("local_model_name") or config.MODEL_NAME
    client = OpenAI(base_url=config.OLLAMA_BASE_URL, api_key="ollama")
    ai.init_client(client, model_name)
    print(f"[OK] Auto-loaded local model: {model_name}")


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    db.create_db()
    auto_init_model()
    print("=" * 60)
    print("🚀 微信自动回复 Web 控制台")
    print("   打开浏览器访问: http://localhost:8000")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
