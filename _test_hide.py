import sys
import time
sys.stdout.reconfigure(encoding="utf-8")

# 完整 App 集成测试：验证真实 window_ids 的隐藏效果
from main import App
import numpy as np
import mss as _mss
_MSS = getattr(_mss, "MSS", _mss.mss)

app = App()

def grab_region(r):
    with _MSS() as sct:
        return np.asarray(sct.grab({"left": r[0], "top": r[1], "width": r[2], "height": r[3]}))[:, :, :3]

def test():
    try:
        # 主面板当前屏幕位置（物理像素）
        fx, fy = app._dpi_fx, app._dpi_fy
        rx, ry = int(app.root.winfo_rootx() / fx), int(app.root.winfo_rooty() / fy)
        rw, rh = int(app.root.winfo_width() / fx), int(app.root.winfo_height() / fy)
        region = (rx, ry, rw, rh)
        print(f"主面板物理区域: {region}")
        print(f"window_ids 数量: {len(app.window_ids)}")

        before = grab_region(region)
        restore = app._hide_own_windows()
        time.sleep(0.05)
        hidden = grab_region(region)
        diff = np.abs(hidden.astype(int) - before.astype(int)).mean()
        print(f"隐藏后画面差异: {diff:.1f}（>5 说明面板已从画面消失 ✓）")
        print(f"隐藏后 std: {hidden.std():.1f} vs 显示中 std: {before.std():.1f}")
        restore()

        # 顺带验证 overlay 也在列表里
        import ctypes
        user32 = ctypes.windll.user32
        for wid in app.window_ids:
            hwnd = user32.GetAncestor(wid, 2) or wid
            vis = bool(user32.IsWindowVisible(hwnd))
            print(f"  winfo_id={hex(wid)} 顶层={hex(hwnd) if hwnd else None} 可见={vis}")
    finally:
        app.root.destroy()

app.root.after(800, test)
app.root.after(3000, lambda: app.root.destroy())
app.root.mainloop()
print("测试结束")
