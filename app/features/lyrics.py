"""歌词模式：从实时翻译结果提取对话主文本（面积最大块），推送到输出面板「歌词」tab。

纯订阅者——只消费实时翻译的结果事件，自身无线程无状态，
实时翻译停止/失败时歌词自然静默，互不影响。"""


class Lyrics:

    def __init__(self, bridge):
        self.bridge = bridge

    def on_translation(self, positioned, lines=None, translated=None):
        """实时翻译每帧完成后被调用：面积最大的译文块 = 对话主文本。"""
        if not positioned:
            return
        main = max(
            positioned,
            key=lambda p: (p["box"][2] - p["box"][0]) * (p["box"][3] - p["box"][1]),
        )
        self.bridge.push("lyrics", main["text"])
