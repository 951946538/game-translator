"""全局热键：RegisterHotKey 实现（无键盘钩子，不拦截/不延迟系统输入，不会误触 Win 键）"""
import ctypes
import ctypes.wintypes
import threading

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# 虚拟键码
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A
VK_LWIN = 0x5B  # 左 Win 键
VK_RWIN = 0x5C  # 右 Win 键


class HotkeyManager:
    """bindings: {VK_键码: 回调函数}"""

    def __init__(self, bindings):
        self.bindings = bindings
        self._thread = None
        self._thread_id = None
        self._user32 = ctypes.windll.user32

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _run(self):
        # RegisterHotKey 必须在持有消息循环的同一线程调用
        ctypes.windll.kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        for hid, vk in enumerate(self.bindings, start=1):
            self._user32.RegisterHotKey(None, hid, 0, vk)  # 无修饰键

        msg = ctypes.wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                hid = msg.wParam
                vk = list(self.bindings.keys())[hid - 1]
                cb = self.bindings[vk]
                try:
                    cb()
                except Exception:
                    pass

        for hid in range(1, len(self.bindings) + 1):
            self._user32.UnregisterHotKey(None, hid)
