"""截图直译 + 问 AI：整屏截图发视觉模型流式翻译 / 文本提问（可带画面）。

与实时翻译完全独立：只在需要干净截帧时调用 ctx.live.clean_grab()。

ctx 需提供：config / bridge / live / wm
"""
import logging
import threading
import time
import traceback

from app import vision


def _thumb_b64(frame, width=520, quality=80):
    """截图帧 → JPEG base64 data URI（推送给 Web UI 历史卡片显示）"""
    try:
        import base64
        import cv2
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, int(h * width / w)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(buf).decode("ascii")
    except Exception:
        return None


class ScreenshotTranslate:

    def __init__(self, ctx):
        self.ctx = ctx

    def translate_now(self):
        """F5：整屏截图直译（流式输出到 Web UI）。"""
        threading.Thread(target=self._translate_worker, daemon=True).start()

    def ask(self, question, with_image=False, image_b64=None):
        """问 AI（Web UI 输入框触发）。image_b64 = 引用的历史截图。"""
        question = (question or "").strip()
        if question:
            threading.Thread(
                target=self._ask_worker, args=(question, with_image, image_b64),
                daemon=True,
            ).start()

    # ---------- 内部 ----------

    def _grab_any_frame(self):
        """抓一帧：有监控区域用 live 的干净抓取（窗口直抓/涂黑），
        没有则整屏截取 + 涂黑自身窗口（截图直译不依赖先选窗口）。"""
        frame = self.ctx.live.clean_grab()
        if frame is not None:
            return frame
        try:
            import mss as _mss
            import numpy as np
            _MSS = getattr(_mss, "MSS", _mss.mss)
            with _MSS() as sct:
                mon = sct.monitors[1]
                shot = sct.grab(mon)
            frame = np.asarray(shot)[:, :, :3]
            for rx, ry, rw, rh in (self.ctx.wm.own_window_rects() or []):
                x1, y1 = max(0, int(rx) - mon["left"]), max(0, int(ry) - mon["top"])
                x2, y2 = min(frame.shape[1], int(rx + rw) - mon["left"]), min(frame.shape[0], int(ry + rh) - mon["top"])
                if x2 > x1 and y2 > y1:
                    frame[y1:y2, x1:x2] = 0
        except Exception:
            logging.error("整屏抓取失败:\n%s", traceback.format_exc())
            return None
        return frame

    def _grab_full_frame(self):
        """F5 专用抓屏：临时隐藏自身窗口，等 DWM 合成更新（250ms）后
        截完整游戏画面（无涂黑块、无工具 UI 混入），截完立即恢复。"""
        n_hidden, restore = self.ctx.wm.hide_visible()
        try:
            if n_hidden:
                time.sleep(0.25)  # 等 DWM 完成合成更新（残影会被截到）
            return self._grab_any_frame()
        finally:
            restore()

    def _stream_to_bridge(self, stream, thumb, question=None):
        """通用流式输出（直译/问答共用）：创建卡片 → 增量推送 → 完成标记"""
        if question is not None:
            self.ctx.bridge.push("ask_start", {"question": question, "thumb": thumb})
        else:
            self.ctx.bridge.push("vision_start", {"thumb": thumb})
        for kind, chunk in stream:
            self.ctx.bridge.push("vision_delta", {"type": kind, "text": chunk})
        self.ctx.bridge.push("vision_done")

    def _translate_worker(self):
        try:
            self.ctx.bridge.push("stage", {"text": "⟳ 截图发送中…", "tone": "warn"})
            frame = self._grab_full_frame()
            if frame is None:
                self.ctx.bridge.push("vision_delta", {"type": "content", "text": "[截图失败]"})
                self.ctx.bridge.push("vision_done")
                return
            self.ctx.bridge.push("stage", {"text": "⟳ 视觉模型翻译中…", "tone": "warn"})
            logging.info("截图直译：发送 %dx%d 给视觉模型（流式）", frame.shape[1], frame.shape[0])
            self._stream_to_bridge(
                vision.translate_screenshot_stream(frame, self.ctx.config),
                _thumb_b64(frame),
            )
            self.ctx.bridge.push("stage", {"text": "✓ 翻译完成", "tone": "success"})
            logging.info("截图直译完成（流式）")
        except Exception as e:
            logging.error("截图直译失败:\n%s", traceback.format_exc())
            self.ctx.bridge.push("vision_delta", {"type": "content", "text": f"\n[截图直译失败: {e}]"})
            self.ctx.bridge.push("vision_done")

    def _ask_worker(self, question, with_image=False, image_b64=None):
        try:
            thumb = None
            frame = None
            if image_b64:
                thumb = image_b64  # 引用历史截图提问：直接用该图
            elif with_image:
                self.ctx.bridge.push("stage", {"text": "⟳ 截图发送中…", "tone": "warn"})
                frame = self._grab_any_frame()
                if frame is not None:
                    thumb = _thumb_b64(frame)

            self.ctx.bridge.push("stage", {"text": "⟳ AI 回答中…", "tone": "warn"})
            self._stream_to_bridge(
                vision.ask_stream(
                    question, frame, self.ctx.config,
                    with_image=with_image or bool(image_b64), image_b64=image_b64,
                ),
                thumb, question=question,
            )
            self.ctx.bridge.push("stage", {"text": "✓ 回答完成", "tone": "success"})
            logging.info("问 AI 完成（%s）", "引用截图" if image_b64 else ("带画面" if with_image else "纯文本"))
        except Exception as e:
            logging.error("问 AI 失败:\n%s", traceback.format_exc())
            self.ctx.bridge.push("vision_delta", {"type": "content", "text": f"\n[问 AI 失败: {e}]"})
            self.ctx.bridge.push("vision_done")
