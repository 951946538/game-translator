# game-translator

游戏实时翻译工具：游玩英文游戏时，框选屏幕区域，工具持续监控画面，**内容变化时自动 OCR 识别并翻译**，译文显示在可拖动的置顶悬浮窗中。

## 工作链路

```
区域截屏监控（帧差检测，画面稳定才触发）
    → PaddleOCR 英文识别
    → 翻译后端（三选一，F10 热切换）
        ① Ollama 本地模型（免费无限量，推荐 qwen2.5:7b 及以上）
        ② LLM 云 API（OpenAI 兼容：DeepSeek / GLM 等，质量最好）
        ③ DeepL API（速度快，免费版每月 50 万字符）
    → 置顶半透明悬浮窗显示译文
```

## 环境要求

- Python **3.12**（PaddleOCR 暂不支持 3.13+；用 `py install 3.12` 安装后按下面步骤建虚拟环境）
- Windows 10/11

## 安装

```powershell
cd j:\项目文件\game-translator
py -V:3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## 使用

```powershell
.venv\Scripts\python main.py
```

| 操作 | 说明 |
|------|------|
| **全屏模式** | F7，直接监控整个主屏并翻译画面中所有英文文本（全屏游戏推荐，游戏请设为**无边框窗口**模式） |
| **框选区域** | F8 或点击面板按钮，拖动框选游戏文本区域（比全屏更快更精准） |
| **暂停/恢复** | F9 |
| **切换翻译后端** | F10（在 enabled_backends 列表内循环，默认 llm ↔ deepl） |
| **译文显示模式** | F11：独立面板 ↔ 覆盖原文位置（译文按 OCR 坐标盖在游戏英文文本上） |
| **移动悬浮窗** | 鼠标按住拖动（面板模式） |

画面变化 → 等待 0.5 秒稳定 → 自动识别翻译，相同文本不重复翻译（内置缓存）。

## 配置（config.json）

| 配置 | 说明 |
|------|------|
| `region` | 监控区域，框选后自动保存，重启免选 |
| `capture_interval_ms` | 截屏间隔，默认 400ms |
| `diff_threshold` | 帧差灵敏度，越小越灵敏（误触发多），默认 4.0 |
| `stable_ms` | 画面稳定判定，默认 500ms |
| `backend` | 当前翻译后端（须在 enabled_backends 内） |
| `enabled_backends` | 启用的后端列表，F10 在列表内循环切换；默认已禁用 ollama，恢复本地模型把 `"ollama"` 加回列表即可 |
| `ollama.model` | 本地模型名，默认 qwen2.5:7b（`ollama pull qwen2.5:7b`） |
| `llm.*` | OpenAI 兼容云 API：base_url / api_key / model |
| `deepl.api_key` | DeepL 密钥（免费版以 `:fx` 结尾，自动走免费域名） |
| `overlay.*` | 悬浮窗透明度 / 宽度 / 字号 |

## 翻译后端建议

- **日常游戏 UI/任务文本**：Ollama 本地（qwen2.5:7b 起步；追求质量用 14b，需显存支持）
- **剧情对白/奇幻术语**：云 LLM（DeepSeek/ GLM）对小模型翻译生硬的内容明显更好
- **追求低延迟**：DeepL

三后端 F10 秒切，遇到难翻文本随手切换对比。

## 目录结构

```
game-translator/
├── main.py               # 入口与调度
├── config.json           # 配置（运行后自动生成）
├── requirements.txt
├── app/
│   ├── config.py         # 配置读写
│   ├── capture.py        # 区域监控 + 帧差检测
│   ├── ocr_engine.py     # PaddleOCR 封装
│   ├── translator.py     # 三后端翻译 + 缓存
│   ├── overlay.py        # 置顶悬浮窗
│   ├── region_select.py  # 屏幕框选
│   └── hotkeys.py        # 全局热键
└── docs/使用说明.md
```
