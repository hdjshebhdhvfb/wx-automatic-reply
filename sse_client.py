"""
WeFlow SSE 消息监听模块
通过 SSE 长连接接收微信新消息推送，替代屏幕截取方案。

SSE 事件格式（来自 WeFlow）:
  event: message.new
  data: {"event":"message.new","sessionId":"wxid_xxx","sessionType":"other",
         "rawid":"1234567890","avatarUrl":"...","sourceName":"张三",
         "content":"你好","timestamp":1760000123}

群聊消息额外包含 groupName 字段。
"""

import json
import queue
import threading
import time
import urllib.request
import urllib.error
from typing import Optional


class SSEClient:
    """SSE 客户端，连接 WeFlow 的消息推送接口，实时接收微信消息。"""

    def __init__(self, url: str):
        self.url = url
        self.message_queue = queue.Queue()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._seen_ids: set = set()
        self._max_seen = 10000
        self.last_error = ""  # 最近一次连接错误，供 Web UI 读取

    # ----------------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------------

    def start(self):
        """启动后台 SSE 监听线程"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self):
        """停止监听"""
        self._running = False

    # ----------------------------------------------------------------
    # 消息获取（主线程调用）
    # ----------------------------------------------------------------

    def get_message(self, timeout: float = 1.0) -> Optional[dict]:
        """非阻塞获取一条消息，超时返回 None。在主线程中调用。"""
        try:
            return self.message_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self):
        """清空消息队列（启动时丢弃积压的旧消息）。"""
        while True:
            try:
                self.message_queue.get_nowait()
            except queue.Empty:
                break

    # ----------------------------------------------------------------
    # 内部：SSE 连接 & 解析
    # ----------------------------------------------------------------

    def _listen(self):
        """后台线程：连接 SSE 流并解析事件"""
        while self._running:
            try:
                req = urllib.request.Request(
                    self.url,
                    headers={
                        'Accept': 'text/event-stream',
                        'Cache-Control': 'no-cache',
                    },
                )
                with urllib.request.urlopen(req) as resp:
                    self.last_error = ""
                    print(f"✅ SSE 已连接 → {self.url}")
                    self._parse_stream(resp)
            except urllib.error.URLError as e:
                if self._running:
                    self.last_error = f"SSE 连接失败: {e}"
                    print(f"⚠️  {self.last_error}，5秒后重连...")
                    time.sleep(5)
            except Exception as e:
                if self._running:
                    self.last_error = f"SSE 连接异常: {e}"
                    print(f"⚠️  {self.last_error}，5秒后重连...")
                    time.sleep(5)

    def _parse_stream(self, resp):
        """逐行解析 SSE 流"""
        current_event = None
        for raw_line in resp:
            if not self._running:
                break
            try:
                line = raw_line.decode('utf-8').rstrip('\n').rstrip('\r')
            except UnicodeDecodeError:
                continue

            # 空行 = 事件结束（SSE 规范）
            if not line:
                current_event = None
                continue

            # 注释行（跳过）
            if line.startswith(':'):
                continue

            if line.startswith('event:'):
                current_event = line[6:].strip()
            elif line.startswith('data:'):
                data_str = line[5:].strip()
                if current_event == 'message.new' and data_str:
                    self._handle_message(data_str)
                current_event = None

    def _handle_message(self, data_str: str):
        """解析单条消息事件并入队"""
        try:
            msg = json.loads(data_str)
        except json.JSONDecodeError:
            return

        # 去重：event + rawid
        dedup_key = f"{msg.get('event', '')}:{msg.get('rawid', '')}"
        if dedup_key in self._seen_ids:
            return
        self._seen_ids.add(dedup_key)
        if len(self._seen_ids) > self._max_seen:
            self._seen_ids.clear()

        self.message_queue.put(msg)
