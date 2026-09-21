"""区域截屏与变化检测：只在画面内容稳定变化后才触发回调，避免重复翻译"""
import logging
import threading
import time
import traceback

import mss
import numpy as np


class RegionMonitor:
    """
    持续监控屏幕区域：
    1. 按间隔截屏，与上一帧做差分
    2. 检测到变化后等待画面稳定（游戏动画/转场会连续变化）
    3. 稳定后回调 on_stable(pil_image)
    """

    def __init__(self, region, interval_ms, diff_threshold, stable_ms, on_stable, force_ocr_ms=2500,
                 trigger_mode="input", input_idle_ms=3000):
        if region == "fullscreen":
            # 全屏模式：监控主显示器整屏
            with mss.mss() as sct:
                mon = sct.monitors[1]  # monitors[0] 是所有显示器的并集，[1] 是主屏
            self.region = {"left": mon["left"], "top": mon["top"],
                           "width": mon["width"], "height": mon["height"]}
        else:
            self.region = {"left": region[0], "top": region[1], "width": region[2], "height": region[3]}
        # 监控区域在屏幕上的偏移（OCR 坐标是相对区域的，绘制时要加回该偏移）
        self.offset = (self.region["left"], self.region["top"])
        self.trigger_mode = trigger_mode      # input: 输入触发 / diff: 持续帧差
        self.input_idle_ms = input_idle_ms    # 输入触发模式：停止操作多久后识别
        self.interval = interval_ms / 1000
        self.diff_threshold = diff_threshold
        self.stable_ms = stable_ms
        self.force_ocr_ms = force_ocr_ms  # 持续变化（滚动/打字机）超过该时长后按最新帧强制识别
        self.on_stable = on_stable

        self.paused = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

        self._prev_small = None       # 上一帧缩略灰度图（用于差分）
        self._pending_frame = None    # 变化后等待稳定的帧
        self._last_change_time = 0.0
        self._pending_since = 0.0     # 待处理帧的起始时间（用于滚动兜底）
        self._last_input = 0.0        # 最近一次点击/滚轮时间（输入触发模式）
        self._input_pending = False
        self._last_processed_small = None  # 上次已识别画面的缩略图

    # ---------- 生命周期 ----------

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False
        self._prev_small = None  # 恢复时重置基准，避免暂停期间的画面变化误判

    def notify_input(self):
        """全局点击/滚轮回调：记录输入活动时间（输入触发模式用）"""
        if not self.paused:
            self._last_input = time.time()
            self._input_pending = True

    # ---------- 内部逻辑 ----------

    def _loop(self):
        with mss.mss() as sct:
            if self.trigger_mode == "input":
                self._input_loop(sct)
            else:
                self._diff_loop(sct)

    def _input_loop(self, sct):
        """
        输入触发模式：点击/滚轮/键盘停止 input_idle_ms（默认3秒）后识别一次。
        画面与上次识别相比没有变化则跳过。

        实现说明：不使用系统级鼠标钩子（pynput listener 是低级钩子，
        每个鼠标移动事件都要过 Python，游戏鼠标 1000Hz 轮询率会拖慢全系统鼠标）。
        改用零开销轮询：
        - GetAsyncKeyState 检测鼠标按键按下
        - GetLastInputInfo 时间戳变化 + 光标位置未变 => 滚轮或键盘操作
          （纯移动鼠标时光标位置会变化，不计为触发输入）
        """
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        VK_MOUSE_BUTTONS = (0x01, 0x02, 0x04, 0x05, 0x06)  # 左/右/中/X1/X2

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        def last_input_tick():
            lii = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
            user32.GetLastInputInfo(ctypes.byref(lii))
            return lii.dwTime

        def any_button_down():
            return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in VK_MOUSE_BUTTONS)

        pt = ctypes.wintypes.POINT()

        def cursor_pos():
            user32.GetCursorPos(ctypes.byref(pt))
            return (pt.x, pt.y)

        prev_tick = last_input_tick()
        prev_pos = cursor_pos()

        while not self._stop.is_set():
            time.sleep(0.25)
            if self.paused:
                continue

            # ---- 输入检测（零钩子开销） ----
            tick = last_input_tick()
            pos = cursor_pos()
            if any_button_down():
                self._last_input = time.time()
                self._input_pending = True
            elif tick != prev_tick and pos == prev_pos:
                # 有新输入但光标没动：滚轮或键盘
                self._last_input = time.time()
                self._input_pending = True
            prev_tick, prev_pos = tick, pos

            # ---- 静置判定 ----
            if not self._input_pending:
                continue
            if (time.time() - self._last_input) * 1000 < self.input_idle_ms:
                continue  # 还在连续操作中，等停下来
            self._input_pending = False

            try:
                shot = sct.grab(self.region)
            except Exception:
                continue
            frame = np.asarray(shot)[:, :, :3]
            small = self._shrink_gray(frame)

            # 与上次已识别画面比对，没变化就不重复识别
            if self._last_processed_small is not None:
                diff = float(np.mean(np.abs(
                    small.astype(np.int16) - self._last_processed_small.astype(np.int16)
                )))
                if diff <= self.diff_threshold:
                    continue
            self._last_processed_small = small

            self._emit_frame(frame)

    def _diff_loop(self, sct):
        while not self._stop.is_set():
            time.sleep(self.interval)
            if self.paused:
                continue
            try:
                shot = sct.grab(self.region)
            except Exception:
                continue

            frame = np.asarray(shot)[:, :, :3]          # RGB
            small = self._shrink_gray(frame)

            if self._prev_small is not None:
                diff = float(np.mean(np.abs(small.astype(np.int16) - self._prev_small.astype(np.int16))))
                if diff > self.diff_threshold:
                    # 画面发生变化，记录并刷新变化时间
                    if self._pending_frame is None:
                        self._pending_since = time.time()
                    self._pending_frame = frame
                    self._last_change_time = time.time()

            self._prev_small = small

            if self._pending_frame is not None:
                now = time.time()
                since_change_ms = (now - self._last_change_time) * 1000
                since_pending_ms = (now - self._pending_since) * 1000

                if since_change_ms >= self.stable_ms:
                    # 画面已稳定：正常触发
                    self._emit()
                elif since_pending_ms >= self.force_ocr_ms:
                    # 内容持续变化（滚动/打字机效果）：
                    # 按最新帧强制识别，并重置计时按此间隔节流
                    self._emit()
                    self._pending_since = now

    def _emit_frame(self, frame):
        logging.info("触发识别")
        try:
            self.on_stable(frame)
        except Exception:
            logging.error("监控回调异常:\n%s", traceback.format_exc())

    def _emit(self):
        logging.info("画面变化，触发识别")
        frame_out = self._pending_frame
        self._pending_frame = None
        try:
            self.on_stable(frame_out)
        except Exception:
            logging.error("监控回调异常:\n%s", traceback.format_exc())

    @staticmethod
    def _shrink_gray(frame, size=(160, 90)):
        """缩小转灰度，加速差分"""
        import cv2  # paddleocr 自带 opencv
        small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        return np.dot(small[..., :3], [0.299, 0.587, 0.114]).astype(np.uint8)
