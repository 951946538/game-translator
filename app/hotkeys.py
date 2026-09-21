"""全局热键：F7 全屏 / F8 选区 / F9 暂停 / F10 切后端 / F11 覆盖/面板切换"""
from pynput import keyboard


class HotkeyManager:
    def __init__(self, on_fullscreen, on_select_region, on_toggle_pause, on_switch_backend, on_toggle_overlay_mode):
        self.listener = keyboard.GlobalHotKeys({
            "<f7>": on_fullscreen,
            "<f8>": on_select_region,
            "<f9>": on_toggle_pause,
            "<f10>": on_switch_backend,
            "<f11>": on_toggle_overlay_mode,
        })

    def start(self):
        self.listener.start()

    def stop(self):
        self.listener.stop()
