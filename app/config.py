"""配置加载与保存（config.json）"""
import json
import os
import threading

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

# 默认配置（首次运行生成 config.json，用户可自行修改）
DEFAULT_CONFIG = {
    "region": None,               # 监控区域 [x, y, w, h]，None 表示尚未选择
    "capture_interval_ms": 400,   # 截屏间隔
    "diff_threshold": 4.0,        # 帧差阈值（均值差，越小越灵敏）
    "stable_ms": 500,             # 画面稳定判定时间（毫秒），变化停止多久后触发 OCR
    "backend": "ollama",          # 当前翻译后端: ollama / llm / deepl
    "ollama": {
        "url": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b",
    },
    "llm": {                      # OpenAI 兼容接口（DeepSeek / GLM / 通义等均可）
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
    },
    "deepl": {
        "api_key": "",            # DeepL 免费版密钥，形如 xxx:fx
    },
    "overlay": {
        "opacity": 0.92,          # 悬浮窗不透明度 0~1
        "max_width": 560,         # 悬浮窗最大宽度（px）
        "font_size": 13,
    },
}


class Config:
    def __init__(self):
        self._lock = threading.Lock()
        self.data = {}
        self.load()

    def load(self):
        with self._lock:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            else:
                self.data = dict(DEFAULT_CONFIG)
                self.save_locked()

    def save(self):
        with self._lock:
            self.save_locked()

    def save_locked(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get(self, *keys, default=None):
        node = self.data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def set(self, value, *keys):
        with self._lock:
            node = self.data
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            node[keys[-1]] = value

    @property
    def region(self):
        r = self.get("region")
        if r == "fullscreen":
            return "fullscreen"
        return tuple(r) if r else None

    @region.setter
    def region(self, value):
        # value: (x,y,w,h) 元组或 "fullscreen" 字符串或 None
        self.set(list(value) if isinstance(value, tuple) else value, "region")
        self.save()
