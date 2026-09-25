"""pywebview 窗口的 Win32 级管理：句柄、透明度、置顶保活、形态定位、截图排除。
此前这些 ctypes 操作散落在 main.py 各处，集中于此避免继续腐化。"""
import ctypes
import ctypes.wintypes as wintypes
import logging
import time
import traceback

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002

# pywebview 窗口标题（与 create_window 的 title 一致）
TITLE_MAIN = "游戏实时翻译"
TITLE_PANEL = "输出面板"

# 输出面板形态（逻辑像素）
PANEL_NORMAL_SIZE = (860, 640)  # 常规面板
PANEL_LYRICS_H = 170            # 歌词横条高度
PANEL_LYRICS_MAX_W = 1500


def _enum_visible_windows_by_title(title):
    user32 = ctypes.windll.user32
    result = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @WNDENUMPROC
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0 and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title:
                result.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return result


def find_hwnd(title):
    """按标题查找可见顶层窗口句柄，找不到返回 0"""
    wins = _enum_visible_windows_by_title(title)
    return wins[0] if wins else 0


# ---------- Win32 帮助函数（集中所有 ctypes 调用，其余模块不再直接碰 user32） ----------

def process_elevated(pid):
    """指定进程是否以管理员权限运行。无法判断返回 None。
    UIPI 规则：普通权限进程无法抓取管理员权限窗口（F7 失败的常见原因）。"""
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    try:
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(h, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
                return None
            try:
                elev = wintypes.DWORD()
                ret_len = wintypes.DWORD()
                if advapi32.GetTokenInformation(token, 20, ctypes.byref(elev), 4, ctypes.byref(ret_len)):
                    return bool(elev.value)  # TokenElevation
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None
    return None


def self_elevated():
    """本进程是否以管理员权限运行"""
    return bool(process_elevated(ctypes.windll.kernel32.GetCurrentProcessId()))


def window_info(hwnd):
    """窗口基本信息（诊断日志用）：标题/类名/PID，失败返回 None"""
    user32 = ctypes.windll.user32
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(n + 1, 64))
        user32.GetWindowTextW(hwnd, buf, n + 1)
        cls_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls_buf, 64)
        return {"title": buf.value, "cls": cls_buf.value, "pid": pid.value}
    except Exception:
        return None


def client_rect(hwnd):
    """窗口客户区的屏幕物理坐标 (x, y, w, h)，失败/过小返回 None"""
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w < 100 or h < 100:
        return None
    return (pt.x, pt.y, w, h)


def window_rect(hwnd):
    """整窗的屏幕物理矩形 (x, y, w, h)，失败返回 None"""
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    return (rect.left, rect.top, w, h)


def grab_window_thumb(hwnd, max_w=280):
    """窗口缩略图（PrintWindow 抓像素 → JPEG base64 data URI）。
    窗口选择器 Alt+Tab 式网格用。失败返回 None。"""
    try:
        import base64
        import ctypes as _ct
        import numpy as np
        import cv2

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        rect = window_rect(hwnd)
        if not rect:
            return None
        _, _, ww, wh = rect

        hdc_win = user32.GetWindowDC(hwnd)
        if not hdc_win:
            return None
        try:
            hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
            hbmp = gdi32.CreateCompatibleBitmap(hdc_win, ww, wh)
            old = gdi32.SelectObject(hdc_mem, hbmp)
            try:
                ok = user32.PrintWindow(hwnd, hdc_mem, 2)  # PW_RENDERFULLCONTENT
                if not ok:
                    gdi32.BitBlt(hdc_mem, 0, 0, ww, wh, hdc_win, 0, 0, 0x00CC0020)

                class _BMI(_ct.Structure):
                    _fields_ = [
                        ("biSize", _ct.c_uint32), ("biWidth", _ct.c_int32),
                        ("biHeight", _ct.c_int32), ("biPlanes", _ct.c_uint16),
                        ("biBitCount", _ct.c_uint16), ("biCompression", _ct.c_uint32),
                        ("biSizeImage", _ct.c_uint32), ("biXPelsPerMeter", _ct.c_int32),
                        ("biYPelsPerMeter", _ct.c_int32), ("biClrUsed", _ct.c_uint32),
                        ("biClrImportant", _ct.c_uint32),
                    ]

                bmi = _BMI()
                bmi.biSize = _ct.sizeof(_BMI)
                bmi.biWidth, bmi.biHeight = ww, wh
                bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
                buf = _ct.create_string_buffer(ww * wh * 4)
                if gdi32.GetDIBits(hdc_mem, hbmp, 0, wh, buf, _ct.byref(bmi), 0) != wh:
                    return None
                img = np.frombuffer(buf, dtype=np.uint8).reshape(wh, ww, 4)
                img = np.flipud(img)[:, :, :3][:, :, ::-1]  # bottom-up BGRA → RGB
                if img.size == 0 or img.std() < 1.0:
                    return None  # 黑图（禁止抓取）
                if ww > max_w:  # 缩到统一宽度（网格整齐 + 编码小）
                    img = cv2.resize(img, (max_w, max(1, round(wh * max_w / ww))))
                ok, buf2 = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 72])
                if not ok:
                    return None
                return "data:image/jpeg;base64," + base64.b64encode(buf2).decode("ascii")
            finally:
                gdi32.SelectObject(hdc_mem, old)
                gdi32.DeleteObject(hbmp)
                gdi32.DeleteDC(hdc_mem)
        finally:
            user32.ReleaseDC(hwnd, hdc_win)
    except Exception:
        return None


class WindowManager:
    """集中管理两个 pywebview 窗口的 Win32 操作。
    wins_provider: () -> {"main": Window, "panel": Window}
    tk_window_ids_provider: () -> [winfo_id, ...]（tk 侧窗口，截图排除/F7 排除用）
    """

    def __init__(self, wins_provider, tk_window_ids_provider):
        self._wins_provider = wins_provider
        self._tk_ids = tk_window_ids_provider
        self.main_hwnd = 0
        self._panel_hwnd = 0
        self.panel_shape = "normal"  # normal / lyrics（「歌词」tab 驱动）
        self.lyrics_shape = None     # 歌词形态尺寸 {"w_ratio": 0.85, "h": 170}（config 传入）

    @property
    def panel_hwnd(self):
        if not self._panel_hwnd:
            self._panel_hwnd = find_hwnd(TITLE_PANEL)
        return self._panel_hwnd

    def get_win(self, name):
        return (self._wins_provider() or {}).get(name)

    # ---------- 透明度 ----------

    def set_alpha(self, title, alpha, hwnd=0):
        """整体透明度（LWA_ALPHA）。hwnd 优先，否则按标题查找。"""
        try:
            hwnd = hwnd or find_hwnd(title)
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if alpha < 1.0 and not (style & WS_EX_LAYERED):
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
            user32.SetLayeredWindowAttributes(hwnd, 0, max(1, int(alpha * 255)), LWA_ALPHA)
        except Exception:
            logging.error("设置窗口透明度失败:\n%s", traceback.format_exc())

    # ---------- 置顶保活 ----------

    def keep_on_top_loop(self):
        """每 5 秒用 Win32 直设置顶（HWND_TOPMOST）。不用 pywebview 的 on_top：
        其内部走 WinForms 属性跨线程赋值不可靠（异常被吞，置顶从未生效）。"""
        user32 = ctypes.windll.user32
        HWND_TOPMOST = -1
        SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
        while True:
            time.sleep(5)
            try:
                for hwnd in (self.main_hwnd, self.panel_hwnd):
                    if hwnd and user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd):
                        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
            except Exception:
                logging.error("置顶循环异常:\n%s", traceback.format_exc())

    # ---------- 输出面板形态与定位 ----------

    def position_main(self):
        """主控窗定位：屏幕右上角（逻辑像素）"""
        win = self.get_win("main")
        if not win:
            return
        try:
            sw = int(win.evaluate_js("screen.width") or 1920)
            win.move(max(20, sw - 320), 20)
        except Exception:
            pass

    def position_panel(self):
        """按当前形态定位输出面板（逻辑像素，pywebview 处理 DPI）。
        lyrics=宽扁横条贴屏幕底部（宽高可调）/ normal=屏幕正中央"""
        win = self.get_win("panel")
        if not win:
            return
        try:
            sw = int(win.evaluate_js("screen.width") or 1920)
            sh = int(win.evaluate_js("screen.height") or 1080)
        except Exception:
            sw, sh = 1920, 1080
        if self.panel_shape == "lyrics":
            shape = self.lyrics_shape or {"w_ratio": 0.85, "h": 170}
            w_ratio = min(0.95, max(0.4, shape.get("w_ratio", 0.85)))
            h = int(min(340, max(110, shape.get("h", 170))))
            w = min(PANEL_LYRICS_MAX_W, int(sw * w_ratio))
            win.resize(w, h)
            # 保持水平居中，垂直贴底（用户拖动过的 x/y 由 sync 接口另行同步）
            win.move((sw - w) // 2, max(20, sh - h - 60))
        else:
            w, h = PANEL_NORMAL_SIZE
            win.resize(w, h)
            win.move((sw - w) // 2, max(20, (sh - h) // 2))

    def panel_window_rect(self):
        """输出面板整窗的屏幕物理矩形（歌词模式监控区域同步用）"""
        hwnd = self.panel_hwnd
        if not hwnd or not ctypes.windll.user32.IsWindowVisible(hwnd):
            return None
        return window_rect(hwnd)

    def set_panel_shape(self, shape):
        """歌词 tab 驱动的形态切换：lyrics（宽扁横条）/ normal（常规面板）。
        歌词形态开启边缘拖拽调整大小（拖完由 resized 事件自动重新框定监控区域）。"""
        if shape == self.panel_shape:
            return
        self.panel_shape = shape
        win = self.get_win("panel")
        try:
            if win:
                win.resizable = (shape == "lyrics")
        except Exception:
            pass
        self.position_panel()
        logging.info("输出面板形态: %s", shape)

    # ---------- 截图排除 ----------

    def own_window_rects(self):
        """本工具自身不透明窗口的屏幕物理矩形列表（截图涂黑排除用）。
        含 tk overlay（跳过全屏透明画布）+ 两个 webview 窗口（可见时）。"""
        user32 = ctypes.windll.user32
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        rects = []
        # tk 侧窗口
        for wid in set(self._tk_ids() or []):
            try:
                hwnd = user32.GetAncestor(wid, 2) or wid
                if not hwnd or not user32.IsWindowVisible(hwnd):
                    continue
                rect = wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    continue
                rw, rh = rect.right - rect.left, rect.bottom - rect.top
                if rw <= 0 or rh <= 0 or (rw >= sw and rh >= sh):
                    continue  # inplace 全屏透明画布跳过
                rects.append((rect.left, rect.top, rw, rh))
            except Exception:
                pass
        # webview 窗口
        for hwnd in (self.main_hwnd, self.panel_hwnd):
            if not hwnd or not user32.IsWindowVisible(hwnd):
                continue
            try:
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.right > rect.left:
                    rects.append((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))
            except Exception:
                pass
        return rects

    def exclude_hwnds(self):
        """F7 前台排除集合（本工具窗口不该被当成游戏捕获）：tk 侧 + webview 侧"""
        user32 = ctypes.windll.user32
        result = set()
        for wid in set(self._tk_ids() or []):
            try:
                result.add(user32.GetAncestor(user32.GetParent(wid), 2))
                result.add(user32.GetAncestor(wid, 2))
            except Exception:
                pass
        for hwnd in (self.main_hwnd, self.panel_hwnd):
            if hwnd:
                result.add(hwnd)
        return result

    def visible_hwnds(self):
        """当前可见的 webview 窗口句柄（F5 截图前临时隐藏用）"""
        user32 = ctypes.windll.user32
        return [h for h in (self.main_hwnd, self.panel_hwnd)
                if h and user32.IsWindowVisible(h)]

    def hide_visible(self):
        """临时隐藏所有可见窗口（F5 整屏截图前，避免自身 UI 混入画面）。
        返回 (隐藏数量, 恢复函数)；恢复函数无条件调用安全。"""
        user32 = ctypes.windll.user32
        hidden = self.visible_hwnds()
        for h in hidden:
            user32.ShowWindow(h, 0)  # SW_HIDE

        def restore():
            for h in hidden:
                try:
                    user32.ShowWindow(h, 5)  # SW_SHOW
                except Exception:
                    pass

        return len(hidden), restore
