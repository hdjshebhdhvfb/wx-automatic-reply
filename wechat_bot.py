"""
微信自动化核心模块 v2.0
支持微信 4.1.9.30+ | 多种消息读取策略

读取消息策略（按优先级）：
  1. UIAutomation — 直接读 UI 控件文本（最快，但新版微信可能不兼容）
  2. 剪贴板选择 — 选中消息→Ctrl+C→读剪贴板（可靠，需鼠标操作）
  3. 截图+OCR  — 截图消息区→OCR识别（最鲁棒，需安装OCR库）

发送消息策略：
  剪贴板粘贴 → Ctrl+V → Enter（最可靠）
"""

import time
import re
import os
import hashlib
from typing import Optional, List, Dict

# ---- 窗口检测 ----
import uiautomation as auto

# ---- 剪贴板 & 键鼠 ----
import pyperclip
import pyautogui
import numpy as np
from PIL import Image

# 安全设置：移动鼠标到屏幕边缘时不会触发异常
pyautogui.FAILSAFE = False


# ============================================================
# 工具函数
# ============================================================

def _clean_text(text: str) -> str:
    """清洗文本"""
    text = re.sub(r'<[^>]*>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ============================================================
# WeChatBot
# ============================================================

class WeChatBot:
    """
    微信自动化机器人。

    用法:
        bot = WeChatBot()
        bot.find_wechat_window()
        bot.open_chat("张三")
        msgs = bot.get_latest_messages()
        bot.send_message("你好！")
    """

    def __init__(self, use_ocr_fallback: bool = False, use_clipboard_reader: bool = True, use_uia_reader: bool = True):
        self.use_ocr_fallback = use_ocr_fallback
        self.use_clipboard_reader = use_clipboard_reader
        self.use_uia_reader = use_uia_reader
        self.main_window: Optional[auto.WindowControl] = None
        self._last_clipboard: str = ""       # 剪贴板去重
        self._last_ocr_hash: str = ""        # 图像哈希去重（比文本比较更可靠）
        self._ocr_engine = None              # 延迟加载

    # ================================================================
    # 1. 窗口检测
    # ================================================================

    def find_wechat_window(self, timeout: int = 10) -> bool:
        """
        查找微信主窗口。尝试多种策略。
        """
        strategies = [
            ("类名匹配", lambda: auto.WindowControl(ClassName='WeChatMainWndForPC')),
            ("标题匹配", lambda: auto.WindowControl(Name='微信', searchDepth=1)),
            ("正则匹配", lambda: auto.WindowControl(RegexName='微信.*', searchDepth=2)),
            ("宽松搜索", lambda: auto.WindowControl(RegexName='微信|WeChat', searchDepth=3)),
        ]

        for label, strategy in strategies:
            try:
                win = strategy()
                if win.Exists(maxSearchSeconds=2):
                    self.main_window = win
                    print(f"✅ 找到微信窗口 [{label}]")
                    print(f"   标题: {win.Name}    类名: {win.ClassName}")
                    return True
            except Exception:
                continue

        print("❌ 未找到微信窗口")
        return False

    def ensure_foreground(self) -> bool:
        """确保微信窗口在前台"""
        if not self.main_window:
            return False
        try:
            self.main_window.SetFocus()
            time.sleep(0.2)
            return True
        except Exception:
            return False

    def get_window_rect(self) -> Optional[tuple]:
        """返回窗口坐标 (left, top, width, height)"""
        if not self.main_window:
            return None
        try:
            r = self.main_window.BoundingRectangle
            return (r.left, r.top, r.width(), r.height())
        except Exception:
            return None

    # ================================================================
    # 2. 联系人 & 聊天导航
    # ================================================================

    def get_contact_list(self) -> List[str]:
        """通过 UIA 获取联系人列表"""
        if not self.main_window:
            return []

        contacts = []
        for sel in [
            lambda: self.main_window.ListControl(Name='会话', searchDepth=5),
            lambda: self.main_window.ListControl(searchDepth=6),
        ]:
            try:
                ctrl = sel()
                if ctrl.Exists():
                    for item in ctrl.GetChildren():
                        name = item.Name.strip()
                        if name and len(name) >= 1:
                            contacts.append(name)
                    if len(contacts) >= 2:
                        break
            except Exception:
                continue

        # 去重
        seen = set()
        return [c for c in contacts if not (c in seen or seen.add(c))]

    def open_chat(self, contact_name: str) -> bool:
        """
        打开指定联系人的聊天。
        方法: Ctrl+F 搜索（最可靠）。
        """
        if not self.main_window:
            return False

        # 方法 1: Ctrl+F 搜索（通用于所有微信版本）
        try:
            self.ensure_foreground()
            pyautogui.hotkey('ctrl', 'f')
            time.sleep(0.25)
            pyperclip.copy(contact_name)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.3)
            pyautogui.press('enter')
            time.sleep(0.4)
            return True
        except Exception:
            pass

        # 方法 2: UIA 列表点击
        try:
            contacts = self.get_contact_list()
            for c in contacts:
                if contact_name in c:
                    item = self.main_window.ListItemControl(Name=c, searchDepth=6)
                    if item.Exists():
                        item.Click()
                        time.sleep(0.4)
                        return True
        except Exception:
            pass

        return False

    # ================================================================
    # 3. 消息读取
    # ================================================================

    # --- 3a. UIA 方式 ------------------------------------------------

    def _read_via_uia(self) -> List[Dict[str, str]]:
        """通过 UIA 控件树读取消息"""
        if not self.main_window:
            return []

        messages = []
        try:
            msg_area = self.main_window.ListControl(Name='消息', searchDepth=6)
            if not msg_area.Exists():
                msg_area = self.main_window.ListControl(searchDepth=8)

            if msg_area.Exists():
                for item in msg_area.GetChildren():
                    text = item.Name
                    if text and len(text.strip()) >= 1:
                        text = _clean_text(text)
                        print(f"  📋 [UIA识别] {text}")
                        messages.append({"sender": "好友", "content": text})
        except Exception:
            pass
        return messages

    # --- 3b. 剪贴板方式 (推荐) ---------------------------------------

    def _read_via_clipboard(self) -> List[Dict[str, str]]:
        """
        通过选中最后一条消息 → Ctrl+C → 读剪贴板来获取消息内容。

        原理:
        1. 鼠标点击消息区域底部（最新消息所在位置）
        2. 连续点击选中消息文本
        3. Ctrl+C 复制
        4. 读取剪贴板
        5. 与上次比较去重
        """
        if not self.main_window:
            return []

        rect = self.get_window_rect()
        if not rect:
            return []

        left, top, width, height = rect

        # 消息区域在窗口右侧约 60% 的位置
        # 最新消息在消息区域底部
        msg_x = left + int(width * 0.60)
        msg_y = top + height - 160

        try:
            # 点击最新消息位置
            pyautogui.click(msg_x, msg_y)
            time.sleep(0.15)

            # 复制选中文本
            pyautogui.hotkey('ctrl', 'c')
            time.sleep(0.15)

            # 读取剪贴板
            content = pyperclip.paste()

            if content and content != self._last_clipboard:
                self._last_clipboard = content
                cleaned = _clean_text(content)
                print(f"  📋 [剪贴板识别] {cleaned}")
                return [{"sender": "好友", "content": cleaned}]

        except Exception as e:
            print(f"⚠️  剪贴板读取失败: {e}")

        return []

    # --- 3c. OCR 方式（基于图像哈希变化检测）-----------------------------

    def _capture_chat_screenshot(self):
        """截取聊天消息区域的截图，返回 PIL Image"""
        rect = self.get_window_rect()
        if not rect:
            return None
        left, top, width, height = rect

        # 截取消息区域（右下方，新消息出现的位置）
        crop_left = max(0, left + int(width * 0.32))
        crop_top = max(0, top + int(height * 0.38))
        crop_width = min(int(width * 0.64), 850)
        crop_height = min(int(height * 0.42), 520)

        try:
            return pyautogui.screenshot(
                region=(crop_left, crop_top, crop_width, crop_height)
            )
        except Exception as e:
            print(f"⚠️  截图失败: {e}")
            return None

    @staticmethod
    def _image_hash(img) -> str:
        """计算图像哈希（缩略图→灰度→MD5），容忍微小变化"""
        thumb = img.resize((48, 36), Image.LANCZOS).convert('L')
        return hashlib.md5(thumb.tobytes()).hexdigest()

    def _ocr_image(self, img) -> str:
        """对图像做 OCR，返回识别到的文本"""
        try:
            arr = np.array(img)

            # EasyOCR: readtext() → [(bbox, text, confidence), ...]
            if hasattr(self._ocr_engine, 'readtext'):
                result = self._ocr_engine.readtext(arr)
                texts = [item[1] for item in result if item[2] > 0.5 and item[1].strip()]
                return "\n".join(texts)

            # PaddleOCR: ocr() → 复杂的嵌套结构
            raw = self._ocr_engine.ocr(arr)
            if raw is None:
                return ""
            texts = []
            if isinstance(raw, list) and len(raw) > 0:
                first = raw[0]
                if first is None:
                    return ""
                if isinstance(first, dict):
                    texts = self._parse_ocr_dict_result(raw)
                elif isinstance(first, list) and len(first) > 0:
                    for item in first:
                        if isinstance(item, (list, tuple)) and len(item) >= 2:
                            entry = item[1]
                            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                                t, c = str(entry[0]), float(entry[1])
                            elif isinstance(entry, str):
                                t, c = entry, 1.0
                            else:
                                continue
                            if float(c) > 0.5 and t.strip():
                                texts.append(t.strip())
                        elif isinstance(item, dict):
                            t = item.get('text', '') or item.get('transcription', '')
                            c = item.get('confidence', 1.0)
                            if float(c) > 0.5 and str(t).strip():
                                texts.append(str(t).strip())
            return "\n".join(texts)

        except Exception as e:
            print(f"⚠️  OCR 识别失败: {e}")
            return ""

    def _read_via_ocr(self) -> List[Dict[str, str]]:
        """
        截图 → 图像哈希比较 → 仅画面变化时才 OCR 并返回新消息。

        图像哈希比文本比较更可靠：
        - 同一画面两次 OCR 不会误判
        - bot 自己的回复会立即更新哈希，不触发死循环
        """
        if self._ocr_engine is None:
            self._ocr_engine = self._init_ocr()
            if self._ocr_engine is None:
                return []

        screenshot = self._capture_chat_screenshot()
        if screenshot is None:
            return []

        # 计算图像哈希，判断画面是否变化
        current_hash = self._image_hash(screenshot)

        if current_hash == self._last_ocr_hash:
            return []  # 画面没变，无新消息

        # 画面变了，OCR 识别内容
        text = self._ocr_image(screenshot)
        if not text:
            self._last_ocr_hash = current_hash
            return []

        self._last_ocr_hash = current_hash
        cleaned = _clean_text(text)
        if cleaned:
            print(f"  📋 [OCR识别] {cleaned}")
        return [{"sender": "好友", "content": cleaned}]

    def _parse_ocr_dict_result(self, raw: list) -> list:
        """解析 PaddleOCR 3.x 的 dict 返回格式"""
        texts = []
        try:
            for page in raw:
                if isinstance(page, dict):
                    # 可能的键: 'rec_texts', 'rec_scores', 'dt_polys' 等
                    rec_texts = page.get('rec_texts', [])
                    rec_scores = page.get('rec_scores', [])
                    for i, t in enumerate(rec_texts):
                        conf = float(rec_scores[i]) if i < len(rec_scores) else 1.0
                        if conf > 0.5 and str(t).strip():
                            texts.append(str(t).strip())
        except Exception:
            pass
        return texts

    def _init_ocr(self):
        """延迟初始化 OCR 引擎"""
        # EasyOCR — 优先使用，API简洁，无需额外深度学习框架
        try:
            import easyocr
            print("🔧 加载 EasyOCR (首次可能较慢，约10-30秒)...")
            return easyocr.Reader(['ch_sim'])
        except ImportError:
            pass

        # PaddleOCR — 备选
        try:
            os.environ.setdefault('FLAGS_enable_pir_api', '0')
            from paddleocr import PaddleOCR
            print("🔧 加载 PaddleOCR (首次可能较慢)...")
            try:
                return PaddleOCR(lang='ch')
            except TypeError:
                pass
            try:
                return PaddleOCR(lang='ch', use_angle_cls=False, show_log=False)
            except Exception:
                return PaddleOCR(lang='ch')
        except ImportError:
            pass

        print("⚠️  未安装 OCR 库。安装方法:")
        print("   pip install easyocr   (推荐，轻量免配置)")
        print("   pip install paddleocr (备选，需 PaddlePaddle)")
        return None

    # --- 统一入口 ----------------------------------------------------

    def get_latest_messages(self) -> List[Dict[str, str]]:
        """
        获取当前聊天窗口的最新消息（自动选择可用策略）。

        返回: [{"sender": "张三", "content": "你好"}, ...]
        """
        # 如果 OCR 引擎已就绪，优先使用 OCR（最可靠）
        if self.use_ocr_fallback and self._ocr_engine is not None:
            msgs = self._read_via_ocr()
            if msgs:
                return msgs

        # 策略: 剪贴板（较快）
        if self.use_clipboard_reader:
            msgs = self._read_via_clipboard()
            if msgs:
                return msgs

        # 策略: UIA（可能不兼容新版微信）
        if self.use_uia_reader:
            msgs = self._read_via_uia()
            if msgs:
                return msgs

        # OCR 未初始化则尝试加载
        if self.use_ocr_fallback and self._ocr_engine is None:
            msgs = self._read_via_ocr()
            if msgs:
                return msgs

        return []

    def seed_cache_for_contact(self, contact_name: str):
        """
        启动时初始化该联系人的消息缓存。
        截取当前聊天画面→计算哈希→标记为"已读"，
        后续只有画面变化（新消息）才会触发回复。
        """
        if not self.main_window:
            return

        self.open_chat(contact_name)
        time.sleep(0.7)

        # 使用图像哈希标记当前画面为"已读"
        screenshot = self._capture_chat_screenshot()
        if screenshot is not None:
            self._last_ocr_hash = self._image_hash(screenshot)

        # 清空剪贴板缓存
        try:
            pyperclip.copy("")
            time.sleep(0.05)
            self._last_clipboard = pyperclip.paste()
        except Exception:
            pass

    def on_reply_sent(self):
        """
        每次 bot 发送回复后必须调用此方法。
        重新截取画面并更新哈希，避免 bot 把自己的回复当成新消息。
        """
        time.sleep(0.5)  # 等消息出现在聊天区
        screenshot = self._capture_chat_screenshot()
        if screenshot is not None:
            self._last_ocr_hash = self._image_hash(screenshot)

    # ================================================================
    # 4. 消息发送
    # ================================================================

    def send_message(self, text: str) -> bool:
        """
        发送消息 — 使用剪贴板+回车（所有微信版本通用）。

        流程: 聚焦输入框 → 清空 → 粘贴 → 回车
        """
        if not text or not text.strip():
            return False

        text = text.strip()

        try:
            # 1. 复制到剪贴板
            pyperclip.copy(text)
            time.sleep(0.05)

            # 2. 聚焦输入框
            self._click_input_area()

            # 3. 粘贴
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.1)

            # 4. 发送
            pyautogui.press('enter')
            time.sleep(0.1)

            return True

        except Exception as e:
            print(f"❌ 发送失败: {e}")
            return False

    def _click_input_area(self):
        """点击输入框区域"""
        rect = self.get_window_rect()
        if rect:
            left, top, width, height = rect
            x = left + int(width * 0.55)
            y = top + height - 50
            pyautogui.click(x, y)
            time.sleep(0.08)
            return

        # 兜底: 尝试 UIA
        if self.main_window:
            try:
                edit = self.main_window.EditControl(searchDepth=8)
                if edit.Exists():
                    edit.Click()
                    return
            except Exception:
                pass

    # ================================================================
    # 5. 新消息检测
    # ================================================================

    def has_new_message(self) -> bool:
        """快速检测是否有新消息（通过剪贴板变化检测）"""
        msgs = self._read_via_clipboard()
        return len(msgs) > 0

    # ================================================================
    # 6. 诊断 & 调试
    # ================================================================

    def dump_ui_tree(self, max_depth: int = 4, output_file: str = None):
        """导出微信 UI 控件树（用于调试新版本适配）"""
        if not self.main_window:
            print("❌ 未连接微信窗口")
            return

        lines = []

        def _walk(ctrl, depth: int = 0):
            if depth > max_depth:
                return
            indent = "  " * depth
            info = (
                f"{indent}[{ctrl.ControlTypeName}] "
                f"Name='{ctrl.Name}' "
                f"Class='{ctrl.ClassName}' "
                f"AutoId='{ctrl.AutomationId}'"
            )
            lines.append(info)
            try:
                for child in ctrl.GetChildren():
                    _walk(child, depth + 1)
            except Exception:
                pass

        _walk(self.main_window)
        output = "\n".join(lines)

        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(output)
            print(f"✅ UI 树已保存: {output_file}")
        else:
            print(output)


# ============================================================
# 诊断入口
# ============================================================

def diagnose():
    """诊断工具：微信连接 + UI树导出"""
    print("=" * 60)
    print("🔍 微信 UI 诊断工具 v2.0")
    print("=" * 60)

    bot = WeChatBot(use_clipboard_reader=True, use_ocr_fallback=False)

    # 1. 窗口
    print("\n[1/3] 查找微信窗口...")
    if not bot.find_wechat_window():
        print("\n❌ 未找到微信窗口，请确认：")
        print("   1. 微信PC版已登录")
        print("   2. 微信窗口未最小化到托盘")
        return

    # 2. 联系人
    print("\n[2/3] 获取联系人列表...")
    contacts = bot.get_contact_list()
    if contacts:
        print(f"   ✅ 找到 {len(contacts)} 个会话")
        for c in contacts[:8]:
            print(f"      - {c}")
        if len(contacts) > 8:
            print(f"      ... 还有 {len(contacts)-8} 个")
    else:
        print("   ⚠️  UIA 无法读取联系人列表")

    # 3. UI 树
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wechat_ui_tree.txt')
    print(f"\n[3/3] 导出 UI 控件树 → {out_path}")
    bot.dump_ui_tree(max_depth=5, output_file=out_path)

    print("\n" + "=" * 60)
    print("✅ 诊断完成！")
    print("💡 建议：")
    print("   1. 先试剪贴板模式 (use_clipboard_reader=True) — 无需额外安装")
    print("   2. 如果剪贴板模式不够用，安装 OCR 引擎并启用 OCR 降级")
    print("   3. 查看 wechat_ui_tree.txt 了解微信 UI 结构")
    print("=" * 60)


if __name__ == '__main__':
    diagnose()
