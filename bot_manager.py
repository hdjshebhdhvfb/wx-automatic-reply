"""
Bot 生命周期管理器 — 后台线程运行机器人，日志通过队列输出。
供 web_server.py 调用，不修改原有 CLI 流程。
"""

import ctypes
import queue
import threading
import time
import sys
import os
from datetime import datetime


def _init_com():
    """在后台线程中初始化 COM，否则 uiautomation 无法操作微信窗口。"""
    try:
        ctypes.windll.ole32.CoInitializeEx(0, 0)  # COINIT_MULTITHREADED
    except Exception:
        pass


def _base_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


sys.path.insert(0, _base_dir())

import config
import ai
import db
from wechat_bot import WeChatBot
from sse_client import SSEClient


class BotManager:
    """管理微信机器人的启动、停止、状态和日志。"""

    MAX_LOG_HISTORY = 200

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.log_queue = queue.Queue()
        self._state = "stopped"       # stopped | starting | running | stopping | error
        self._model_name = ""
        self._listeners: list = []
        self._start_time: datetime | None = None
        self._error_msg = ""
        self._last_error = ""         # 最近一次异常信息，Web UI 仪表盘展示
        self._log_history: list = []  # 保留最近日志，新 SSE 客户端连接时回放

    # ----------------------------------------------------------------
    # 公共属性
    # ----------------------------------------------------------------

    @property
    def status(self) -> dict:
        uptime = 0
        if self._start_time:
            uptime = (datetime.now() - self._start_time).total_seconds()
        return {
            "state": self._state,
            "model": self._model_name,
            "listeners": self._listeners,
            "uptime": int(uptime),
            "error": self._error_msg,
            "last_error": self._last_error,
        }

    @property
    def state(self) -> str:
        return self._state

    # ----------------------------------------------------------------
    # 启停控制
    # ----------------------------------------------------------------

    def start(self, listeners: list, use_sse: bool = True) -> bool:
        """启动机器人。返回 True 表示成功。"""
        if self._state == "running":
            return False

        self._stop_event.clear()
        self._state = "starting"
        self._error_msg = ""
        self._last_error = ""
        self._listeners = listeners
        self._start_time = datetime.now()

        target = self._run_sse_loop if use_sse else self._run_polling_loop

        def _thread_wrapper():
            _init_com()
            target()

        self._thread = threading.Thread(target=_thread_wrapper, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """停止机器人。"""
        if self._state not in ("running", "starting"):
            return
        self._log("🛑 正在停止机器人...")
        self._state = "stopping"
        self._stop_event.set()
        # 线程会在下次循环检测到 stop_event 后退出

    # ----------------------------------------------------------------
    # 内部 — 日志
    # ----------------------------------------------------------------

    def _log(self, msg: str):
        """写日志到队列，同时保留历史用于新客户端回放。"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        self.log_queue.put(line)
        self._log_history.append(line)
        if len(self._log_history) > self.MAX_LOG_HISTORY:
            self._log_history = self._log_history[-self.MAX_LOG_HISTORY:]

    # ----------------------------------------------------------------
    # 内部 — 微信连接
    # ----------------------------------------------------------------

    def _connect_wechat(self) -> WeChatBot | None:
        """尝试连接微信窗口。"""
        for i in range(3):
            bot = WeChatBot(
                use_uia_reader=config.USE_UIA_READER,
                use_clipboard_reader=config.USE_CLIPBOARD_READER,
                use_ocr_fallback=config.USE_OCR_FALLBACK,
            )
            if bot.find_wechat_window():
                self._log(f"✅ 微信连接成功 (第 {i + 1} 次尝试)")
                return bot
            self._log(f"⚠️  第 {i + 1} 次失败，2秒后重试...")
            time.sleep(2)
        return None

    # ----------------------------------------------------------------
    # 内部 — 消息处理
    # ----------------------------------------------------------------

    def _process_message(self, bot: WeChatBot, contact_name: str, content: str,
                          chat_to_open: str = None, reply_prefix: str = ''):
        """处理单条消息：AI 生成回复 → 发送 → 记录。"""
        now_str = datetime.now().strftime("%H:%M:%S")

        if not content or not content.strip():
            return

        display = contact_name
        if chat_to_open and chat_to_open != contact_name:
            display = f"{chat_to_open}→{contact_name}"

        self._log(f"📩 收到 [{display}]: {content}")
        self._log(f"🤖 正在生成回复...")

        try:
            reply = ai.chat(contact_name, content)

            if reply_prefix and not reply.startswith(reply_prefix.strip()):
                reply = f"{reply_prefix}{reply}"

            self._log(f"✅ AI 回复 [{display}]: {reply}")

            if config.REPLY_DELAY > 0:
                time.sleep(config.REPLY_DELAY)

            target_chat = chat_to_open or contact_name
            bot.open_chat(target_chat)
            time.sleep(0.3)

            if bot.send_message(reply):
                self._log(f"📤 已发送 → {target_chat}")
                db.add_history(contact_name, "user", content)
                db.add_history(contact_name, "assistant", reply)
            else:
                self._log(f"❌ 发送失败")

        except Exception as e:
            self._log(f"❌ 处理失败: {e}")

    # ----------------------------------------------------------------
    # 内部 — SSE 主循环
    # ----------------------------------------------------------------

    def _run_sse_loop(self):
        """SSE 推送模式主循环（后台线程）。"""
        self._log("🔍 正在连接微信...")
        bot = self._connect_wechat()
        if bot is None:
            self._log("❌ 无法连接微信，请确认微信已登录且窗口未最小化")
            self._state = "error"
            self._error_msg = "微信连接失败"
            return

        try:
            sse = SSEClient(config.WEFLOW_SSE_URL)
            sse.start()
            time.sleep(0.5)
            sse.flush()

            last_msg_cache = {}
            pending = {}

            self._state = "running"
            self._last_error = ""
            self._log(f"✅ 系统启动成功 (SSE 模式)")
            self._log(f"🤖 模型: {ai.MODEL}")
            self._log(f"👥 监听: {', '.join(self._listeners)}")

            while not self._stop_event.is_set():
                try:
                    msg = sse.get_message(timeout=1.0)
                    # SSE 连接错误由 SSEClient 内部捕获，这里轮询读取
                    if sse.last_error:
                        self._last_error = sse.last_error
                    elif self._last_error and self._last_error == sse.last_error:
                        self._last_error = ""  # 错误已被 SSEClient 清除
                    now = time.time()

                    # ---- 收集消息到缓冲区 ----
                    if msg is not None:
                        source_name = msg.get('sourceName', '')
                        content = msg.get('content', '').strip()
                        session_type = msg.get('sessionType', 'other')
                        group_name = msg.get('groupName', '')
                        session_id = msg.get('sessionId', '')

                        if source_name and content:
                            is_group = session_type == 'group' and group_name
                            if is_group:
                                if group_name not in self._listeners and source_name not in self._listeners:
                                    continue
                            else:
                                if source_name not in self._listeners:
                                    continue

                            cache_key = f"{session_id}:{source_name}:{content}"
                            if cache_key == last_msg_cache.get(session_id):
                                continue
                            last_msg_cache[session_id] = cache_key

                            if content.startswith('[') and content.endswith(']'):
                                continue

                            if is_group:
                                ai_user = source_name
                                chat_to_open = group_name
                                reply_prefix = f"@{source_name} "
                            else:
                                ai_user = source_name
                                chat_to_open = source_name
                                reply_prefix = ''

                            if ai_user not in pending:
                                pending[ai_user] = {
                                    'msgs': [], 'chat_to_open': chat_to_open, 'reply_prefix': reply_prefix
                                }
                                self._log(f"⏳ 收到 [{ai_user}] 消息，等待 {config.MESSAGE_MERGE_DELAY}s 缓冲...")
                            else:
                                count = len(pending[ai_user]['msgs']) + 1
                                self._log(f"   [{ai_user}] 缓冲中 ({count} 条)")

                            pending[ai_user]['msgs'].append((now, content))

                    # ---- 处理到期缓冲区 ----
                    to_process = []
                    for uid in list(pending.keys()):
                        entry = pending[uid]
                        msgs = entry.get('msgs', [])
                        if msgs and now - msgs[0][0] >= config.MESSAGE_MERGE_DELAY:
                            to_process.append(uid)

                    for uid in to_process:
                        entry = pending.pop(uid)
                        msgs = entry.get('msgs', [])
                        if not msgs:
                            continue
                        merged = '\n'.join(m[1] for m in msgs if len(m) >= 2)
                        if not merged:
                            continue
                        self._process_message(
                            bot, uid, merged,
                            chat_to_open=entry.get('chat_to_open'),
                            reply_prefix=entry.get('reply_prefix', ''),
                        )

                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    self._last_error = str(e)
                    self._log(f"⚠️ 主循环异常: {e} (缓冲数: {len(pending)})")
                    time.sleep(2)

            # ---- 退出处理 (SSE) ----
            for uid, entry in pending.items():
                msgs = entry.get('msgs', [])
                if not msgs:
                    continue
                merged = '\n'.join(m[1] for m in msgs if len(m) >= 2)
                if not merged:
                    continue
                self._log(f"🔄 处理缓冲区 [{uid}]: {len(msgs)} 条消息")
                self._process_message(
                    bot, uid, merged,
                    chat_to_open=entry.get('chat_to_open'),
                    reply_prefix=entry.get('reply_prefix', ''),
                )

            sse.stop()

        except Exception as e:
            self._log(f"❌ 启动失败: {e}")
            self._state = "error"
            self._error_msg = str(e)
            return

        self._state = "stopped"
        self._start_time = None
        self._log("👋 机器人已停止")

    # ----------------------------------------------------------------
    # 内部 — 轮询主循环
    # ----------------------------------------------------------------

    def _run_polling_loop(self):
        """传统轮询模式主循环（后台线程）。"""
        self._log("🔍 正在连接微信...")
        bot = self._connect_wechat()
        if bot is None:
            self._log("❌ 无法连接微信，请确认微信已登录且窗口未最小化")
            self._state = "error"
            self._error_msg = "微信连接失败"
            return

        try:
            if config.USE_OCR_FALLBACK:
                self._log("🔧 预加载 OCR + 缓存当前消息...")
                for name in self._listeners:
                    bot.seed_cache_for_contact(name)
                self._log("✅ 缓存完成")

            last_msg_cache = {}
            pending = {}

            self._state = "running"
            self._last_error = ""
            self._log(f"✅ 系统启动成功 (轮询模式)")
            self._log(f"🤖 模型: {ai.MODEL}")
            self._log(f"👥 监听: {', '.join(self._listeners)}")
            self._log(f"⏱️  间隔: {config.POLL_INTERVAL}s")

            while not self._stop_event.is_set():
                try:
                    now = time.time()
                    # 正常运行，清除旧错误
                    if self._last_error:
                        self._last_error = ""

                    for name in self._listeners:
                        if self._stop_event.is_set():
                            break
                        bot.open_chat(name)
                        time.sleep(0.5)

                        messages = bot.get_latest_messages()
                        if not messages:
                            continue

                        latest = messages[-1] if messages else {}
                        content = latest.get("content", "").strip() if isinstance(latest, dict) else ""
                        if not content:
                            continue

                        cache_key = f"{name}:{content}"
                        if cache_key == last_msg_cache.get(name):
                            continue
                        last_msg_cache[name] = cache_key

                        if content.startswith("[") and content.endswith("]"):
                            continue

                        if name not in pending:
                            pending[name] = {'msgs': [], 'chat_to_open': name, 'reply_prefix': ''}
                            self._log(f"⏳ 收到 [{name}] 消息，等待 {config.MESSAGE_MERGE_DELAY}s 缓冲...")
                        else:
                            count = len(pending[name]['msgs']) + 1
                            self._log(f"   [{name}] 缓冲中 ({count} 条)")

                        pending[name]['msgs'].append((now, content))

                    # 处理到期缓冲区
                    to_process = []
                    for uid in list(pending.keys()):
                        entry = pending[uid]
                        msgs = entry.get('msgs', [])
                        if msgs and now - msgs[0][0] >= config.MESSAGE_MERGE_DELAY:
                            to_process.append(uid)

                    for uid in to_process:
                        entry = pending.pop(uid)
                        msgs = entry.get('msgs', [])
                        if not msgs:
                            continue
                        merged = '\n'.join(m[1] for m in msgs if len(m) >= 2)
                        if not merged:
                            continue
                        self._process_message(bot, uid, merged)

                    time.sleep(config.POLL_INTERVAL)

                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    self._last_error = str(e)
                    self._log(f"⚠️ 主循环异常: {e}")
                    time.sleep(2)

            # 退出处理
            for uid, entry in pending.items():
                msgs = entry.get('msgs', [])
                if not msgs:
                    continue
                merged = '\n'.join(m[1] for m in msgs if len(m) >= 2)
                if not merged:
                    continue
                self._log(f"🔄 处理缓冲区 [{uid}]: {len(msgs)} 条消息")
                self._process_message(bot, uid, merged)

        except Exception as e:
            self._log(f"❌ 启动失败: {e}")
            self._state = "error"
            self._error_msg = str(e)
            return

        self._state = "stopped"
        self._start_time = None
        self._log("👋 机器人已停止")


# 全局单例
bot_manager = BotManager()
