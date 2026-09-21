"""全局热键：F7 全屏模式 / F8 重新选区 / F9 暂停恢复 / F10 切换翻译后端"""
from pynput import keyboard


class HotkeyManager:
    def __init__(self, on_fullscreen, on_select_region, on_toggle_pause, on_switch_backend):
        self.listener = keyboard.GlobalHotKeys({
            "<f7>": on_fullscreen,
            "<f8>": on_select_region,
            "<f9>": on_toggle_pause,
            "<f10>": on_switch_backend,
        })

    def start(self):
        self.listener.start()

    def stop(self):
        self.listener.stop()
