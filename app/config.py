"""配置加载与保存（config.json）"""
import json
import os
import threading

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

# 默认配置（首次运行生成 config.json，用户可自行修改）
DEFAULT_CONFIG = {
    "region": None,               # 监控区域 [x, y, w, h]，None 表示尚未选择
    "capture_interval_ms": 1000,  # 截屏间隔（帧差模式用）
    "trigger_mode": "diff",       # 触发模式：diff 画面变化触发（推荐）/ input 点击停止3秒后识别
    "input_idle_ms": 3000,        # 输入触发模式：停止操作多久后识别
    "ocr_high_accuracy": False,   # true 使用 server 高精度模型（准但慢5~10倍）；默认 medium 快速模型
    "disable_win_key": False,     # true 时智能屏蔽 Win 键：仅游戏窗口前台时拦截（防误触），切出游戏正常使用
    "diff_threshold": 4.0,        # 帧差阈值（均值差，越小越灵敏）
    "stable_ms": 500,             # 画面稳定判定时间（毫秒），变化停止多久后触发 OCR
    "force_ocr_ms": 2500,         # 滚动兜底：内容持续变化超过该时长，按最新帧强制识别
    "min_translate_interval_ms": 5000,  # 翻译节流：两次自动翻译的最小间隔（毫秒），防止过于频繁
    "ocr_min_score": 0.6,         # OCR 置信度过滤：低于该分数的结果丢弃（过滤艺术字体噪声）
    "overlay_pad": 6,             # 覆盖块向四周扩大的像素数（盖住检测框外的字体边缘）
    "auto_translate": True,       # 自动翻译开关（F7 进入游戏窗口时自动置为 False，F9 切换）
    "overlay_mode": "inplace",    # 译文显示：panel 独立面板 / inplace 覆盖原文位置（F11 切换）
    "llm": {                      # OpenAI 兼容接口（DeepSeek / GLM / 通义等均可）
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
    },
    "vision": {                   # 截图直译（视觉模型，F5）；api_key 留空复用 llm 段
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-v4-flash-vision-exp",
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
