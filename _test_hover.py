import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
import ctypes
import ctypes.wintypes

user32 = ctypes.windll.user32


def get_rect(hwnd):
    r = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def click(x, y):
    user32.SetCursorPos(x, y)
    time.sleep(0.3)
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


proc = subprocess.Popen([sys.executable, "main.py"])
time.sleep(10)  # 等启动 + BOOT_TIME 防护过期

hwnd = 0
with open("game-translator.log", encoding="utf-8") as f:
    for line in f:
        if "webview 窗口句柄" in line:
            hwnd = int(line.split("句柄: ")[1].split(",")[0])
print(f"hwnd = {hwnd}")
if not hwnd:
    proc.kill()
    sys.exit(1)

r = get_rect(hwnd)
print(f"初始(应为收起态): {r.left},{r.top} {r.right - r.left}x{r.bottom - r.top}")

# 点击右缘竖条
tx, ty = r.right - 10, (r.top + r.bottom) // 2
print(f"点击竖条: {tx},{ty}")
click(tx, ty)
time.sleep(1.5)

r2 = get_rect(hwnd)
print(f"点击后(应展开): {r2.left},{r2.top} {r2.right - r2.left}x{r2.bottom - r2.top}")
expanded = (r2.right - r2.left) > (r.right - r.left) * 1.5
print("展开:", "✓ 成功" if expanded else "✗ 失败")

# 鼠标移开窗口（触发自动收起）
user32.SetCursorPos(50, 50)
time.sleep(1.5)
r3 = get_rect(hwnd)
print(f"移开后(应收起): {r3.left},{r3.top} {r3.right - r3.left}x{r3.bottom - r3.top}")
print("收起:", "✓ 成功" if (r3.right - r3.left) < (r2.right - r2.left) * 0.6 else "✗ 失败")

proc.kill()
