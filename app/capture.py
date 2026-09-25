"""区域截屏与变化检测：画面稳定变化后才触发回调，避免重复翻译。

抓取优先级：游戏窗口直抓（PrintWindow，悬浮窗永不混入）
         → dxcam DXGI 桌面复制（独占全屏/HDR）
         → mss GDI 截屏 + 自身窗口矩形涂黑。
"""
import ctypes
import logging
import threading
import time
import traceback

import mss
import numpy as np

_MSS = getattr(mss, "MSS", mss.mss)  # 兼容 mss 9.x(mss) / 10.x(MSS)

# ---------- DXGI 桌面复制引擎（dxcam）：独占全屏游戏 / HDR 下 GDI 截屏会得到黑图，
# DXGI Desktop Duplication 是游戏录制工具的标准方案，两种场景都能抓 ----------
_dxcam = None
_dxcam_failed = False


def _dxcam_grab(region):
    """DXGI 抓屏，失败或不可用返回 None（调用方回退 mss）。region 为 mss 字典格式。"""
    global _dxcam, _dxcam_failed
    if _dxcam_failed:
        return None
    try:
        if _dxcam is None:
            import dxcam
            _dxcam = dxcam.create(output_color="BGR")
            if _dxcam is None:
                _dxcam_failed = True
                return None
        left, top = region["left"], region["top"]
        frame = _dxcam.grab(region=(left, top, left + region["width"], top + region["height"]))
        if frame is None:
            return None
        return frame[:, :, ::-1]  # BGR → RGB
    except Exception:
        _dxcam_failed = True
        logging.warning("dxcam 不可用，后续使用 mss 抓屏:\n%s", traceback.format_exc())
        return None


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class RegionMonitor:
    """
    持续监控屏幕区域（帧差模式）：
    1. 按间隔截屏，与上一帧做差分
    2. 检测到变化后等待画面稳定（游戏动画/转场会连续变化）
    3. 稳定后回调 on_stable(frame, origin, force, scene_reset)
    """

    def __init__(self, region, interval_ms, diff_threshold, stable_ms, on_stable,
                 force_ocr_ms=2500, min_interval_ms=0, game_hwnd=0, exclude_rects_provider=None):
        if region == "fullscreen":
            with _MSS() as sct:
                mon = sct.monitors[1]  # monitors[0] 是所有显示器的并集，[1] 是主屏
            self.region = {"left": mon["left"], "top": mon["top"],
                           "width": mon["width"], "height": mon["height"]}
        else:
            self.region = {"left": region[0], "top": region[1], "width": region[2], "height": region[3]}
        # 监控区域在屏幕上的偏移（OCR 坐标是相对区域的，绘制时要加回该偏移）
        self.offset = (self.region["left"], self.region["top"])
        self.interval = interval_ms / 1000
        self.diff_threshold = diff_threshold
        self.stable_ms = stable_ms
        self.force_ocr_ms = force_ocr_ms      # 持续变化（滚动/打字机）超时后按最新帧强制识别
        self.min_interval = min_interval_ms / 1000  # 两次自动翻译的最小间隔（节流）
        self.game_hwnd = game_hwnd or 0        # 游戏窗口句柄：非零时直接从窗口抓取
        self.exclude_rects_provider = exclude_rects_provider  # 自身窗口矩形（涂黑排除）
        self.on_stable = on_stable

        self.paused = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._diff_loop, daemon=True)

        self._prev_small = None       # 上一帧缩略灰度图（差分基准）
        self._pending_frame = None    # 变化后等待稳定的帧
        self._last_change_time = 0.0
        self._pending_since = 0.0     # 待处理帧的起始时间（滚动兜底）
        self._prev_emit_small = None  # 上次已识别画面的网格灰度图（变化区域裁剪用）
        self._last_emit_time = 0.0    # 上次触发翻译的时间（节流用）

    # ---------- 生命周期 ----------

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False
        self._prev_small = None  # 恢复时重置基准，避免暂停期间的变化误判

    def capture_now(self):
        """F6 手动触发：立即整帧识别翻译。"""
        try:
            frame = self.clean_grab()
            if frame is None:
                return
            logging.info("手动触发识别 (F6)")
            self._emit_frame(frame, force=True)
        except Exception:
            logging.error("手动触发失败:\n%s", traceback.format_exc())

    # ---------- 监控循环 ----------

    def _diff_loop(self):
        while not self._stop.is_set():
            time.sleep(self.interval)
            if self.paused:
                continue
            try:
                frame = self.raw_grab()
                if frame is None:
                    continue
                small = self._gray(frame, self._DIFF_SIZE)

                if self._prev_small is not None and self._frame_changed(small, self._prev_small):
                    if self._pending_frame is None:
                        self._pending_since = time.time()
                    self._pending_frame = frame
                    self._last_change_time = time.time()

                self._prev_small = small

                if self._pending_frame is not None:
                    now = time.time()
                    since_change_ms = (now - self._last_change_time) * 1000
                    since_pending_ms = (now - self._pending_since) * 1000
                    since_emit_s = now - self._last_emit_time

                    stable = since_change_ms >= self.stable_ms
                    forced = since_pending_ms >= self.force_ocr_ms

                    # 节流：间隔不足时保留待处理帧，等间隔到了再触发（不漏翻）
                    if (stable or forced) and since_emit_s >= self.min_interval:
                        self._emit()
                        if forced and not stable:
                            self._pending_since = now
            except Exception:
                # 任何异常都不允许杀死监控线程
                logging.error("帧差监控循环异常:\n%s", traceback.format_exc())
                time.sleep(1)

    # ---------- 抓取 ----------

    def _grab_window(self):
        """直接从游戏窗口抓取客户区像素（PrintWindow/BitBlt，OBS 窗口捕获原理）。
        悬浮在游戏上方的任何窗口（包括本工具面板）都不会出现在画面里。
        失败（窗口关闭/游戏禁止抓取返回黑图）返回 None。"""
        hwnd = self.game_hwnd
        if not hwnd:
            return None
        try:
            import ctypes.wintypes as wintypes
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            if not user32.IsWindow(hwnd):
                logging.warning("游戏窗口已关闭，回退屏幕截图模式")
                self.game_hwnd = 0
                return None

            cr = wintypes.RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(cr)):
                return None
            cw, ch = cr.right, cr.bottom
            if cw < 50 or ch < 50:
                return None

            wr = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(wr))
            ww, wh = wr.right - wr.left, wr.bottom - wr.top

            hdc_win = user32.GetWindowDC(hwnd)
            if not hdc_win:
                return None
            try:
                hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
                hbmp = gdi32.CreateCompatibleBitmap(hdc_win, ww, wh)
                old = gdi32.SelectObject(hdc_mem, hbmp)
                try:
                    # PW_RENDERFULLCONTENT=2：DirectX/DWM 渲染内容（现代游戏）也能抓到
                    ok = user32.PrintWindow(hwnd, hdc_mem, 2)
                    if not ok:
                        gdi32.BitBlt(hdc_mem, 0, 0, ww, wh, hdc_win, 0, 0, 0x00CC0020)

                    bmi = _BITMAPINFOHEADER()
                    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
                    bmi.biWidth = ww
                    bmi.biHeight = wh
                    bmi.biPlanes = 1
                    bmi.biBitCount = 32
                    bmi.biCompression = 0  # BI_RGB
                    buf = ctypes.create_string_buffer(ww * wh * 4)
                    n_scan = gdi32.GetDIBits(hdc_mem, hbmp, 0, wh, buf, ctypes.byref(bmi), 0)
                    if n_scan != wh:
                        return None
                    # bottom-up BGRA → top-down RGB
                    frame = np.frombuffer(buf, dtype=np.uint8).reshape(wh, ww, 4)
                    frame = np.flipud(frame)[:, :, :3][:, :, ::-1].copy()

                    # 裁剪客户区（客户区原点相对窗口左上角）
                    pt = wintypes.POINT(0, 0)
                    user32.ClientToScreen(hwnd, ctypes.byref(pt))
                    cx, cy = max(0, pt.x - wr.left), max(0, pt.y - wr.top)
                    frame = frame[cy:cy + ch, cx:cx + cw]

                    # 全黑检测：部分游戏/驱动禁止抓取，返回纯黑图
                    if frame.size == 0 or frame.std() < 1.0:
                        logging.warning("窗口抓取返回黑图（游戏可能禁止抓取），回退屏幕截图")
                        self.game_hwnd = 0
                        return None
                    return frame
                finally:
                    gdi32.SelectObject(hdc_mem, old)
                    gdi32.DeleteObject(hbmp)
                    gdi32.DeleteDC(hdc_mem)
            finally:
                user32.ReleaseDC(hwnd, hdc_win)
        except Exception:
            logging.error("窗口抓取异常，回退屏幕截图:\n%s", traceback.format_exc())
            return None

    def _screen_grab_raw(self):
        """屏幕抓帧（未涂黑）：dxcam(DXGI) 优先，黑图/失败自动回退 mss。"""
        frame = _dxcam_grab(self.region)
        if frame is not None and frame.std() >= 1.0:
            return frame
        with _MSS() as sct:
            shot = sct.grab(self.region)
        return np.asarray(shot)[:, :, :3]

    def _mask_own_windows(self, frame):
        """自身窗口覆盖的矩形涂黑——面板文字在像素层面被抹掉，无时序依赖。"""
        if not self.exclude_rects_provider:
            return
        try:
            rects = self.exclude_rects_provider() or []
        except Exception:
            return
        ox, oy = self.offset
        h, w = frame.shape[:2]
        for rx, ry, rw, rh in rects:
            x1, y1 = max(0, int(rx) - ox), max(0, int(ry) - oy)
            x2, y2 = min(w, int(rx + rw) - ox), min(h, int(ry + rh) - oy)
            if x2 > x1 and y2 > y1:
                frame[y1:y2, x1:x2] = 0

    def raw_grab(self):
        """常规截帧（帧差检测用）：窗口直抓 → 截屏+涂黑。"""
        frame = self._grab_window()
        if frame is not None:
            return frame
        frame = self._screen_grab_raw()
        self._mask_own_windows(frame)
        return frame

    def clean_grab(self):
        """出帧抓取（送 OCR/视觉模型）：窗口直抓 → 截屏+涂黑。"""
        frame = self._grab_window()
        if frame is not None:
            return frame
        frame = self._screen_grab_raw()
        self._mask_own_windows(frame)
        return frame

    # ---------- 出帧 ----------

    def _emit_frame(self, frame, force=False):
        """裁剪变化区域后触发识别（对话更新时只识别那一小块，OCR 量降为几分之一）。
        force=True（F6）整帧识别，配合上层清空旧译文完整重建。"""
        now = time.time()
        if not force and now - self._last_emit_time < self.min_interval:
            return  # 自动翻译节流；F6 手动触发不受限
        self._last_emit_time = now
        h, w = frame.shape[:2]
        if force:
            x0, y0, x1, y1 = 0, 0, w, h
        else:
            x0, y0, x1, y1 = self._change_bbox(frame)
            margin = 24
            x0 = max(0, x0 - margin)
            y0 = max(0, y0 - margin)
            x1 = min(w, x1 + margin)
            y1 = min(h, y1 + margin)
            # 变化区域过大（转场/换页）或异常时退化为整帧
            if x1 <= x0 or y1 <= y0 or (x1 - x0) * (y1 - y0) > 0.75 * w * h:
                x0, y0, x1, y1 = 0, 0, w, h

        self._prev_emit_small = self._gray(frame, self._GRID)
        crop = frame[y0:y1, x0:x1]
        origin = (self.region["left"] + x0, self.region["top"] + y0)
        # 整帧识别（手动或变化区域>75%）= 转场/换页：上层据此清空旧译文
        scene_reset = force or (x1 - x0) * (y1 - y0) > 0.75 * w * h
        logging.info("触发识别：%dx%d（占画面 %.0f%%）", x1 - x0, y1 - y0,
                     100 * (x1 - x0) * (y1 - y0) / (w * h))
        try:
            self.on_stable(crop, origin, force, scene_reset=scene_reset)
        except Exception:
            logging.error("监控回调异常:\n%s", traceback.format_exc())

    def _emit(self):
        frame_out = self._pending_frame
        self._pending_frame = None
        # 出帧时重新干净截取（待处理帧是常规截图，可能含面板内容）
        clean = self.clean_grab()
        if clean is not None:
            frame_out = clean
        if frame_out is not None:
            self._emit_frame(frame_out)

    # ---------- 帧差判定 ----------

    # 变化检测网格（宽 x 高）
    _GRID = (80, 45)

    # 帧差判定参数：320x180 网格逐格比对（全局均值在 4K 屏上检测不到单行文字变化）
    _DIFF_SIZE = (320, 180)
    _DIFF_CELL_THRESHOLD = 12   # 单格灰度差超过该值视为变化
    _DIFF_MIN_RATIO = 0.0015    # 变化格子占比超过 0.15%（约一行文字）即触发

    def _frame_changed(self, a, b):
        """逐格差分判定画面是否变化（对单行文字变化敏感）"""
        diff_map = np.abs(a.astype(np.int16) - b.astype(np.int16))
        return (diff_map > self._DIFF_CELL_THRESHOLD).mean() > self._DIFF_MIN_RATIO

    def _change_bbox(self, frame):
        """与上次已识别帧网格级比对，返回变化区域包围盒（全分辨率坐标）"""
        h, w = frame.shape[:2]
        small = self._gray(frame, self._GRID)
        if self._prev_emit_small is None:
            return 0, 0, w, h
        diff = np.abs(small.astype(np.int16) - self._prev_emit_small.astype(np.int16))
        changed = diff > 8
        if changed.sum() < 2 or changed.mean() > 0.7:
            return 0, 0, w, h
        ys, xs = np.where(changed)
        gx, gy = w / self._GRID[0], h / self._GRID[1]
        return (int(xs.min() * gx), int(ys.min() * gy),
                int((xs.max() + 1) * gx), int((ys.max() + 1) * gy))

    @staticmethod
    def _gray(frame, size):
        """缩小转灰度，加速差分"""
        import cv2  # paddleocr 自带 opencv
        small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        return np.dot(small[..., :3], [0.299, 0.587, 0.114]).astype(np.uint8)
