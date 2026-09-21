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

    def __init__(self, region, interval_ms, diff_threshold, stable_ms, on_stable):
        if region == "fullscreen":
            # 全屏模式：监控主显示器整屏
            with mss.mss() as sct:
                mon = sct.monitors[1]  # monitors[0] 是所有显示器的并集，[1] 是主屏
            self.region = {"left": mon["left"], "top": mon["top"],
                           "width": mon["width"], "height": mon["height"]}
        else:
            self.region = {"left": region[0], "top": region[1], "width": region[2], "height": region[3]}
        self.interval = interval_ms / 1000
        self.diff_threshold = diff_threshold
        self.stable_ms = stable_ms
        self.on_stable = on_stable

        self.paused = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

        self._prev_small = None       # 上一帧缩略灰度图（用于差分）
        self._pending_frame = None    # 变化后等待稳定的帧
        self._last_change_time = 0.0

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

    # ---------- 内部逻辑 ----------

    def _loop(self):
        with mss.mss() as sct:
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
                        self._pending_frame = frame
                        self._last_change_time = time.time()

                self._prev_small = small

                # 有待处理帧且画面已稳定
                if (
                    self._pending_frame is not None
                    and (time.time() - self._last_change_time) * 1000 >= self.stable_ms
                ):
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
