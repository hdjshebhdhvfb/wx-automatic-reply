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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import db
import ai
from wechat_bot import WeChatBot


# ============================================================
# 初始化
# ============================================================

def load_names() -> list:
    """加载 names.txt 中的好友列表"""
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.NAMES_FILE)

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

def process_message(bot: WeChatBot, contact_name: str, content: str):
    """处理单条消息：AI 生成回复 → 发送 → 更新图像哈希"""
    now = datetime.now().strftime("%H:%M:%S")

    if not content or not content.strip():
        return

    print(f"\n{'─' * 55}")
    print(f"  📩 [{now}] 收到 | {contact_name}")
    print(f"     消息: {content}")
    print(f"  🤖 正在生成回复...")

    try:
        reply = ai.chat(contact_name, content)

        print(f"  ✅ [{now}] AI 生成回复:")
        print(f"     回复: {reply}")

        if config.REPLY_DELAY > 0:
            time.sleep(config.REPLY_DELAY)

        bot.open_chat(contact_name)
        time.sleep(0.3)

        if bot.send_message(reply):
            print(f"  📤 [{now}] 已发送 → {contact_name}")
            print(f"{'─' * 55}")
            db.add_history(contact_name, "user", content)
            db.add_history(contact_name, "assistant", reply)

            # 关键：回复发出后立即更新图像哈希，避免后续把自己回复当成新消息
            bot.on_reply_sent()
        else:
            print(f"  ❌ [{now}] 发送失败")
            print(f"{'─' * 55}")

    except Exception as e:
        print(f"  ❌ [{now}] 处理失败: {e}")
        print(f"{'─' * 55}")


# ============================================================
# 主循环
# ============================================================

def main_loop(bot: WeChatBot, listen_names: list):
    """主消息循环"""

    last_msg_cache = {}  # {name: last_content}  去重用

    # ---- 启动时预初始化 OCR + 缓存当前消息（避免回复旧消息） ----
    if config.USE_OCR_FALLBACK:
        print("\n🔧 预加载 OCR 引擎 + 缓存当前消息...")
        for name in listen_names:
            print(f"   📋 缓存 [{name}] 的当前消息...")
            bot.seed_cache_for_contact(name)
        print("✅ 缓存完成，只回复新消息\n")

    print("\n" + "=" * 60)
    print(f"✅ 系统启动成功")
    print(f"🤖 模型: {config.MODEL_NAME}")
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
            for name in listen_names:
                # 打开聊天
                bot.open_chat(name)
                time.sleep(0.5)

                # 获取消息
                messages = bot.get_latest_messages()
                if not messages:
                    continue

                # 取最新一条
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

                process_message(bot, name, content)

            time.sleep(config.POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n" + "=" * 60)
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

    # 4. 连接微信
    bot = init_wechat()

    # 5. 主循环
    main_loop(bot, names)


if __name__ == '__main__':
    main()
