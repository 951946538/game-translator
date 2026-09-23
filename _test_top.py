import sys
import subprocess
import time
import ctypes
import ctypes.wintypes as wt

sys.stdout.reconfigure(encoding="utf-8")
user32 = ctypes.windll.user32

p = subprocess.Popen([sys.executable, "main.py"])
time.sleep(10)

result = []
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


@WNDENUMPROC
def cb(hwnd, _):
    n = user32.GetWindowTextLengthW(hwnd)
    if n > 0:
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value in ("游戏实时翻译", "输出面板") and user32.IsWindowVisible(hwnd):
            ex = user32.GetWindowLongW(hwnd, -20)
            result.append((buf.value, bool(ex & 0x8)))  # WS_EX_TOPMOST
    return True


user32.EnumWindows(cb, 0)
if not result:
    print("未找到目标窗口")
for name, top in result:
    mark = "OK" if top else "FAIL"
    print(f"{name}: topmost={mark}")
p.kill()
