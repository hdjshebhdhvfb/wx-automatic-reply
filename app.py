"""
DeepSeek 微信自动回复系统 v2.0
支持微信 4.1.9.30+

用法:
  1. 编辑 names.txt，每行写一个好友名字
  2. 确保 Ollama 运行中: ollama serve
  3. 运行: python app.py

消息读取策略（自动选择）:
  剪贴板模式 (默认) → 选中最后消息 → Ctrl+C → 读剪贴板
  UIA 模式 → 直接读微信 UI 控件
  OCR 模式 → 截图 → OCR 识别 (需安装 paddleocr)
"""

import time
import sys
import os
from datetime import datetime


def _base_dir() -> str:
    """返回程序所在目录（兼容 PyInstaller 打包和源码运行）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


sys.path.insert(0, _base_dir())

import config
import db
import ai
from wechat_bot import WeChatBot
from sse_client import SSEClient
from api_selector import select_model


# ============================================================
# 初始化
# ============================================================

def load_names() -> list:
    """加载 names.txt 中的好友列表"""
    file_path = os.path.join(_base_dir(), config.NAMES_FILE)

    if not os.path.exists(file_path):
        print(f"\n⚠️  配置文件不存在: {file_path}")
        print("💡 已创建模板文件，请编辑后重新运行")
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("# 每行写一个要监听的好友名字\n")
            f.write("# 例如:\n")
            f.write("# 张三\n")
            f.write("# 李四\n")
        return None

    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    names = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#'):
            names.append(line)
    return names


def init_wechat() -> WeChatBot:
    """连接微信"""
    print("🔍 正在连接微信...")

    for i in range(3):
        bot = WeChatBot(
            use_uia_reader=config.USE_UIA_READER,
            use_clipboard_reader=config.USE_CLIPBOARD_READER,
            use_ocr_fallback=config.USE_OCR_FALLBACK,
        )
        if bot.find_wechat_window():
            print(f"✅ 微信连接成功 (第 {i+1} 次尝试)")
            return bot

        print(f"⚠️  第 {i+1} 次失败，2秒后重试...")
        time.sleep(2)

    print("\n❌ 无法连接微信，请确认：")
    print("   1. 微信PC版已登录")
    print("   2. 微信窗口已打开（不要最小化到托盘）")
    print("\n💡 运行诊断: python wechat_bot.py")
    input("\n按回车键退出...")
    sys.exit(1)


# ============================================================
# 消息处理
# ============================================================

def process_message(bot: WeChatBot, contact_name: str, content: str,
                    chat_to_open: str = None, reply_prefix: str = '',
                    need_hash_update: bool = True):
    """
    处理单条消息：AI 生成回复 → 发送 → 记录历史。

    Args:
        contact_name: 消息发送者（AI 上下文的用户名）
        content: 消息内容
        chat_to_open: 要打开的微信聊天名称（默认同 contact_name）
        reply_prefix: 回复前缀（群聊时用于 @发送者）
        need_hash_update: 是否更新 OCR 图像哈希（SSE 模式下无需更新）
    """
    now = datetime.now().strftime("%H:%M:%S")

    if not content or not content.strip():
        return

    # 用 chat_to_open 作为显示名（群聊时显示群名+发送者）
    display_sender = f"{chat_to_open}→{contact_name}" if chat_to_open and chat_to_open != contact_name else contact_name

    print(f"\n{'─' * 55}")
    print(f"  📩 [{now}] 收到 | {display_sender}")
    print(f"     消息: {content}")
    print(f"  🤖 正在生成回复...")

    try:
        reply = ai.chat(contact_name, content)

        # 群聊时加 @前缀
        if reply_prefix and not reply.startswith(reply_prefix.strip()):
            reply = f"{reply_prefix}{reply}"

        print(f"  ✅ [{now}] AI 生成回复:")
        print(f"     回复: {reply}")

        if config.REPLY_DELAY > 0:
            time.sleep(config.REPLY_DELAY)

        target_chat = chat_to_open or contact_name
        bot.open_chat(target_chat)
        time.sleep(0.3)

        if bot.send_message(reply):
            print(f"  📤 [{now}] 已发送 → {target_chat}")
            print(f"{'─' * 55}")
            db.add_history(contact_name, "user", content)
            db.add_history(contact_name, "assistant", reply)

            if need_hash_update:
                bot.on_reply_sent()
        else:
            print(f"  ❌ [{now}] 发送失败")
            print(f"{'─' * 55}")

    except Exception as e:
        print(f"  ❌ [{now}] 处理失败: {e}")
        print(f"{'─' * 55}")


# ============================================================
# 主循环（SSE 模式 — 通过 WeFlow 推送接收消息）
# ============================================================

def main_loop_sse(bot: WeChatBot, listen_names: list):
    """基于 SSE 推送的主消息循环：WeFlow 实时推送 → AI 回复 → 微信发送"""

    sse = SSEClient(config.WEFLOW_SSE_URL)
    sse.start()
    time.sleep(0.5)

    # 丢弃启动前的积压消息，避免回复旧消息
    sse.flush()

    # 去重缓存
    last_msg_cache = {}

    # 消息合并缓冲区: {user_id: {'msgs': [(ts, content), ...], 'chat_to_open': ..., 'reply_prefix': ...}}
    pending = {}

    print("\n" + "=" * 60)
    print(f"✅ 系统启动成功")
    print(f"🤖 模型: {ai.MODEL}")
    print(f"👥 监听: {', '.join(listen_names)}")
    print(f"📡 消息来源: WeFlow SSE 推送")
    print(f"💡 按 Ctrl+C 安全退出")
    print("=" * 60 + "\n")

    while True:
        try:
            msg = sse.get_message(timeout=1.0)
            now = time.time()

            # ---- 收集消息到缓冲区 ----
            if msg is not None:
                source_name = msg.get('sourceName', '')
                content = msg.get('content', '').strip()
                session_type = msg.get('sessionType', 'other')
                group_name = msg.get('groupName', '')
                session_id = msg.get('sessionId', '')

                if source_name and content:
                    # 匹配监听列表
                    is_group = session_type == 'group' and group_name
                    if is_group:
                        if group_name not in listen_names and source_name not in listen_names:
                            continue
                    else:
                        if source_name not in listen_names:
                            continue

                    # 去重
                    cache_key = f"{session_id}:{source_name}:{content}"
                    if cache_key == last_msg_cache.get(session_id):
                        continue
                    last_msg_cache[session_id] = cache_key

                    # 跳过媒体占位消息
                    if content.startswith('[') and content.endswith(']'):
                        continue

                    # 群聊：打开群 → @发送者；私聊：直接打开联系人
                    if is_group:
                        ai_user = source_name
                        chat_to_open = group_name
                        reply_prefix = f"@{source_name} "
                    else:
                        ai_user = source_name
                        chat_to_open = source_name
                        reply_prefix = ''

                    # 放入缓冲区
                    if ai_user not in pending:
                        pending[ai_user] = {
                            'msgs': [],
                            'chat_to_open': chat_to_open,
                            'reply_prefix': reply_prefix,
                        }
                    pending[ai_user]['msgs'].append((now, content))

            # ---- 处理到期的缓冲区（最早消息已等待 >= MESSAGE_MERGE_DELAY 秒） ----
            to_process = []
            for user_id in list(pending.keys()):
                entry = pending[user_id]
                oldest_ts = entry['msgs'][0][0]
                if now - oldest_ts >= config.MESSAGE_MERGE_DELAY:
                    to_process.append(user_id)

            for user_id in to_process:
                entry = pending.pop(user_id)
                merged_content = '\n'.join(m[1] for m in entry['msgs'])

                process_message(
                    bot, contact_name=user_id, content=merged_content,
                    chat_to_open=entry['chat_to_open'],
                    reply_prefix=entry['reply_prefix'],
                    need_hash_update=False,
                )

        except KeyboardInterrupt:
            print("\n\n" + "=" * 60)
            # 退出前处理所有缓冲中的消息
            for user_id, entry in pending.items():
                merged_content = '\n'.join(m[1] for m in entry['msgs'])
                print(f"🔄 处理缓冲区 [{user_id}]: {len(entry['msgs'])} 条消息")
                process_message(
                    bot, contact_name=user_id, content=merged_content,
                    chat_to_open=entry['chat_to_open'],
                    reply_prefix=entry['reply_prefix'],
                    need_hash_update=False,
                )
            print("👋 收到退出信号，安全关闭中...")
            print("=" * 60)
            sse.stop()
            break

        except Exception as e:
            print(f"\n⚠️  主循环异常: {e}")
            print("💡 2秒后自动恢复...")
            time.sleep(2)

def main_loop(bot: WeChatBot, listen_names: list):
    """主消息循环"""

    last_msg_cache = {}  # {name: last_content}  去重用
    pending = {}          # 消息合并缓冲区

    # ---- 启动时预初始化 OCR + 缓存当前消息（避免回复旧消息） ----
    if config.USE_OCR_FALLBACK:
        print("\n🔧 预加载 OCR 引擎 + 缓存当前消息...")
        for name in listen_names:
            print(f"   📋 缓存 [{name}] 的当前消息...")
            bot.seed_cache_for_contact(name)
        print("✅ 缓存完成，只回复新消息\n")

    print("\n" + "=" * 60)
    print(f"✅ 系统启动成功")
    print(f"🤖 模型: {ai.MODEL}")
    print(f"👥 监听: {', '.join(listen_names)}")
    print(f"⏱️  间隔: {config.POLL_INTERVAL}s")
    # 显示当前启用的读取方式
    active_modes = []
    if bot.use_uia_reader:
        active_modes.append("UIA")
    if bot.use_clipboard_reader:
        active_modes.append("剪贴板")
    if bot.use_ocr_fallback:
        active_modes.append("OCR")
    print(f"📋 消息读取: {' + '.join(active_modes) if active_modes else '无'}")
    print(f"💡 按 Ctrl+C 安全退出")
    print("=" * 60 + "\n")

    while True:
        try:
            now = time.time()

            # ---- 收集消息到缓冲区 ----
            for name in listen_names:
                bot.open_chat(name)
                time.sleep(0.5)

                messages = bot.get_latest_messages()
                if not messages:
                    continue

                latest = messages[-1]
                content = latest.get("content", "").strip()
                if not content:
                    continue

                # 去重
                cache_key = f"{name}:{content}"
                if cache_key == last_msg_cache.get(name):
                    continue
                last_msg_cache[name] = cache_key

                # 跳过系统提示
                if content.startswith("[") and content.endswith("]"):
                    continue

                # 放入缓冲区
                if name not in pending:
                    pending[name] = {'msgs': [], 'chat_to_open': name, 'reply_prefix': ''}
                pending[name]['msgs'].append((now, content))

            # ---- 处理到期的缓冲区 ----
            to_process = []
            for user_id in list(pending.keys()):
                entry = pending[user_id]
                oldest_ts = entry['msgs'][0][0]
                if now - oldest_ts >= config.MESSAGE_MERGE_DELAY:
                    to_process.append(user_id)

            for user_id in to_process:
                entry = pending.pop(user_id)
                merged_content = '\n'.join(m[1] for m in entry['msgs'])

                process_message(bot, user_id, merged_content)

            time.sleep(config.POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n" + "=" * 60)
            # 退出前处理缓冲中的消息
            for user_id, entry in pending.items():
                merged_content = '\n'.join(m[1] for m in entry['msgs'])
                print(f"🔄 处理缓冲区 [{user_id}]: {len(entry['msgs'])} 条消息")
                process_message(bot, user_id, merged_content)
            print("👋 收到退出信号，安全关闭中...")
            print("=" * 60)
            break

        except Exception as e:
            print(f"\n⚠️  主循环异常: {e}")
            print("💡 2秒后自动恢复...")
            time.sleep(2)


# ============================================================
# 入口
# ============================================================

def main():
    print("=" * 60)
    print("🚀  DeepSeek 微信自动回复系统 v2.0")
    print("    支持微信 4.1.9.30+")
    print("=" * 60)

    # 1. 数据库
    db.create_db()

    # 2. 好友列表
    names = load_names()
    if names is None:
        sys.exit(1)
    if not names:
        print(f"\n⚠️  {config.NAMES_FILE} 中没有有效的好友名称")
        input("\n按回车键退出...")
        sys.exit(1)

    # 3. 注册 AI 用户
    for name in names:
        ai.add_user(name)

    # 3.5 模型选择
    _client, model_name = select_model()
    ai.init_client(_client, model_name)

    # 4. SSE 模式：不需要微信窗口操作，直接监听推送即可
    if config.WEFLOW_SSE_ENABLED:
        print(f"\n📡 SSE 模式已启用")
        print(f"   推送地址: {config.WEFLOW_SSE_URL}")

        # 尝试连接微信（用于发送回复）
        bot = init_wechat()

        # 启动 SSE 主循环
        main_loop_sse(bot, names)
        return

    # 5. 传统模式：连接微信 → 轮询消息
    bot = init_wechat()

    # 6. 主循环
    main_loop(bot, names)


if __name__ == '__main__':
    main()
