# DeepSeek 微信自动回复系统 v2.0

基于 **WeFlow** 和 **Ollama** 的微信 AI 自动回复机器人。通过 WeFlow SSE 实时推送接收微信消息，调用 Ollama 本地部署的 DeepSeek 模型生成回复，再通过 Windows UI 自动化将回复发送回微信。

## 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10 / 11 (64-bit) |
| 微信版本 | 微信 PC 版 4.1.9.30 及以上 |
| Python | 3.9+ |
| Ollama | 最新版，用于本地运行 DeepSeek 模型 |
| WeFlow | 需运行在本地，提供 SSE 消息推送和 [HTTP API](https://github.com/hicccc77/WeFlow/blob/main/docs/HTTP-API.md) |

### Ollama 要求

- 下载安装 [Ollama](https://ollama.com/download)
- 拉取推荐模型：`ollama pull deepseek-r1:7b`
- 也支持其他兼容 OpenAI API 的模型（如 qwen2.5:7b、deepseek-r1:14b）
- 确保 Ollama 服务正在运行（默认监听 `localhost:11434`）

### WeFlow 要求

-下载安装[WeFlow](https://github.com/hicccc77/WeFlow)
- WeFlow 需运行在本地，提供微信消息实时推送能力
- 默认 SSE 推送地址：`http://127.0.0.1:5031/api/v1/push/messages`
- 需在 `config.py` 中配置正确的 Access Token

## 依赖

### 核心依赖（必装）

| 包 | 用途 |
|------|------|
| `openai >= 1.0.0` | 调用 Ollama API（OpenAI 兼容端点） |
| `uiautomation >= 2.0.0` | Windows UI 自动化，检测微信窗口和控件 |
| `pyperclip >= 1.8.0` | 系统剪贴板读写 |
| `pyautogui >= 0.9.0` | 截图、鼠标移动和键盘模拟 |

## 安装步骤

```bash
# 1. 克隆仓库
git clone <https://github.com/hdjshebhdhvfb/wx-automatic-reply>
cd wx2

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 安装 Ollama 并拉取模型
ollama pull deepseek-r1:7b

# 4. （可选）安装 OCR 支持，提升消息读取准确性
pip install easyocr
```

### 验证安装

```bash
# 验证 Python 依赖
python -c "import openai; import uiautomation; import pyautogui; import PIL; print('✅ 依赖正常')"

# 验证 Ollama 模型
ollama list

# 验证 Ollama API
curl http://localhost:11434/api/tags

# 验证微信连接（需微信已登录且窗口打开）
python wechat_bot.py
```

## 功能说明

- **SSE 实时消息推送** — 通过 WeFlow SSE 长连接实时接收微信新消息，无需轮询屏幕
- **AI 智能回复** — 调用 Ollama 本地 DeepSeek 模型，基于上下文对话历史生成自然回复
- **多联系人监听** — 支持同时监控多个好友，每个联系人维护独立会话上下文
- **群聊支持** — 支持监听群聊消息并 @发送者 回复
- **对话历史持久化** — 基于 SQLite 存储聊天记录，重启后保留会话上下文
- **多种消息读取策略** — 支持 UIA 控件读取、剪贴板复制、OCR 截图识别三种模式
- **自然回复延迟** — 可配置回复延迟，模拟真人打字节奏
- **去重机制** — 消息级去重，避免重复回复
- **可定制 AI 风格** — 通过 `config.py` 中的系统提示词自定义回复风格

## 使用方法

### 1. 配置好友列表

编辑 `names.txt`，每行写一个要监听的好友名称：

```
张三
李四
王五
```

### 2. 修改配置（可选）

编辑 `config.py` 按需调整：

- `MODEL_NAME` — AI 模型名称（默认 `deepseek-r1:7b`）
- `POLL_INTERVAL` — 传统模式轮询间隔（默认 1 秒）
- `REPLY_DELAY` — 回复延迟（默认 0.5 秒，模拟真人）
- `TEMPERATURE` — AI 回复创意度（0~1，默认 0.7）
- `SYSTEM_PROMPT` — 自定义 AI 回复风格
- `LISTEN_MODE` — 监听模式（`specific` 仅指定好友 / `all` 所有人）

### 3. 启动运行

```bash
# 1. 确保 Ollama 正在运行
ollama serve

# 2. 确保微信 PC 版已登录，窗口处于打开状态

# 3. 确保 WeFlow 正在运行

# 4. 启动程序
python app.py

# 5. 按 Ctrl+C 安全退出
```

### 4. 使用打包好的 EXE

下载 Release 中的 `wx2.exe`，将 `names.txt` 放在同一目录下，双击运行即可（无需安装 Python）。

## 架构设计

```
WeFlow SSE 推送
      │
      ▼
  sse_client.py ─── 接收实时消息推送
      │
      ▼
  app.py ─── 主控逻辑：消息匹配 → AI 调用 → 发送回复
      │
      ├── ai.py ──── 通过 Ollama API 调用本地 DeepSeek 模型
      ├── db.py ──── SQLite 聊天历史持久化
      ├── config.py ─ 全局配置
      └── wechat_bot.py ─ 微信窗口自动化（查找窗口 / 读消息 / 发送消息）
```

### 消息读取策略（wechat_bot.py）

| 策略 | 原理 | 优点 | 缺点 |
|------|------|------|------|
| UIA | 直接读取微信 UI 控件文本 | 最快 | 随微信版本变化可能失效 |
| 剪贴板 | 点击消息 → Ctrl+C → 读取剪贴板 | 通用可靠 | 需要鼠标操作 |
| OCR | 截图消息区域 → OCR 识别 | 最鲁棒 | 速度较慢，需额外依赖 |

### 两种运行模式

| 模式 | 消息来源 | 适用场景 |
|------|----------|----------|
| **SSE 模式**（默认） | WeFlow 实时推送 | 推荐，无需操作微信窗口即可接收消息 |
| **传统轮询模式** | 屏幕截取/剪贴板读取 | 无 WeFlow 环境时的备选方案 |

## 文件说明

| 文件 | 说明 |
|------|------|
| `app.py` | 主程序入口 |
| `wechat_bot.py` | 微信自动化核心（含诊断工具） |
| `ai.py` | AI 回复模块（通过 Ollama 调用本地模型） |
| `db.py` | SQLite 聊天历史持久化 |
| `sse_client.py` | WeFlow SSE 消息监听客户端 |
| `config.py` | 全局配置文件 |
| `names.txt` | 监听好友列表 |
| `requirements.txt` | Python 依赖清单 |

## License

MIT
