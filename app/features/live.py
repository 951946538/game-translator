"""实时翻译：区域监控 → PaddleOCR → LLM 翻译 → 覆盖层/独立面板显示。

独立流水线：监控线程（RegionMonitor 内）→ OCR 线程 → 翻译线程，世代号机制
作废旧任务（F6 手动翻译/转场检测时，飞行中的旧结果不写回）。

ctx 需提供：config / translator / bridge / ui_queue / wm /
  dpi_factors → (fx, fy) / overlay_mode / overlay_hidden /
  status(text) / refresh_status() / schedule_stage_revert() /
  on_translation(positioned, lines, translated)
"""
import logging
import queue
import threading
import time
import traceback

from app.capture import RegionMonitor
from app.ocr_engine import OCREngine
from app.textblock import group_lines
from app.uifilter import is_own_ui_text

BLOCK_TTL_S = 90   # 译文块过期时间：场景切换漏检时的最终兜底（残留最多存在 90 秒）
MAX_BLOCKS = 30    # 覆盖层历史块上限


class LiveTranslate:

    def __init__(self, ctx):
        self.ctx = ctx
        self.monitor = None
        self.ocr_engine = None
        self._ocr_lock = threading.Lock()
        self.ocr_queue = queue.Queue()
        self.translate_queue = queue.Queue()
        self.positioned_history = []  # 覆盖层译文累积（跨帧保留未变化区域旧译文）
        self._last_key = None         # 去重：与上一帧相同文本集合不重复翻译
        self._gen = 0                 # 世代号：重置时递增，流水线旧任务结果作废
        self.paused = not ctx.config.get("auto_translate", default=True)

        threading.Thread(target=self._ocr_worker, daemon=True).start()
        threading.Thread(target=self._translate_worker, daemon=True).start()
        threading.Thread(target=self._expiry_loop, daemon=True).start()
        threading.Thread(target=self._preload_ocr, daemon=True).start()

    # ---------- 对外接口（App 调用） ----------

    def start(self, region, game_hwnd=None):
        """（重新）开始监控。region 为物理像素 (x,y,w,h) 或 'fullscreen'。"""
        self.stop()
        if not region:
            return
        cfg = self.ctx.config
        self.monitor = RegionMonitor(
            region=region,
            interval_ms=cfg.get("capture_interval_ms", default=400),
            diff_threshold=cfg.get("diff_threshold", default=4.0),
            stable_ms=cfg.get("stable_ms", default=500),
            on_stable=self._on_stable_frame,
            force_ocr_ms=cfg.get("force_ocr_ms", default=2500),
            min_interval_ms=cfg.get("min_translate_interval_ms", default=5000),
            game_hwnd=game_hwnd or 0,
            exclude_rects_provider=self.ctx.wm.own_window_rects,
        )
        self.monitor.start()
        if self.paused:
            self.monitor.pause()

    def stop(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor = None

    def capture_now(self):
        """F6：立即整帧重译当前画面。"""
        if self.monitor:
            self.monitor.capture_now()

    def set_paused(self, value, persist=True):
        self.paused = value
        if persist:
            self.ctx.config.set(not value, "auto_translate")
            self.ctx.config.save()
        if self.monitor:
            if value:
                self.monitor.pause()
            else:
                self.monitor.resume()

    def toggle_pause(self):
        """F9：切换自动翻译，返回新状态。"""
        self.set_paused(not self.paused)
        return self.paused

    def clean_grab(self):
        """干净截帧（截图直译功能共用入口）：无监控时返回 None。"""
        return self.monitor.clean_grab() if self.monitor else None

    def clear(self):
        """清空译文历史与显示（F7 换区域 / F11 切显示模式时调用）。"""
        self.positioned_history = []
        self._last_key = None
        self._bump_gen()
        self.ctx.ui_queue.put(("overlay_clear", None))

    # ---------- 内部流水线 ----------

    def _bump_gen(self):
        """作废流水线中所有旧任务：递增世代号 + 清空积压队列（保留退出哨兵）。"""
        self._gen += 1
        for q in (self.ocr_queue, self.translate_queue):
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    q.put(None)
                    break

    def _on_stable_frame(self, frame, origin, force=False, scene_reset=False):
        """监控线程回调。force=F6 手动；scene_reset=转场/换页（变化区域>75%）。
        两者都清空旧译文并作废飞行中任务。"""
        if not self.paused or force:
            if force or scene_reset:
                logging.info("重置译文（%s）：清空 %d 块旧译文",
                             "F6手动" if force else "转场/换页", len(self.positioned_history))
                self.positioned_history = []
                self._last_key = None
                self._bump_gen()
                self.ctx.ui_queue.put(("overlay_clear", None))
            self.ctx.bridge.push("stage", {"text": "⟳ 正在识别…", "tone": "warn"})
            self.ocr_queue.put((frame, origin, self._gen))

    def _get_ocr(self):
        """惰性初始化 OCR 引擎（线程安全）。"""
        with self._ocr_lock:
            if self.ocr_engine is None:
                self.ctx.status("正在加载 PaddleOCR 模型（首次约 10-30 秒）…")
                cfg = self.ctx.config
                self.ocr_engine = OCREngine(
                    lang="en",
                    min_score=cfg.get("ocr_min_score", default=0.6),
                    high_accuracy=cfg.get("ocr_high_accuracy", default=False),
                )
                self.ctx.refresh_status()
            return self.ocr_engine

    def _ocr_worker(self):
        """OCR 线程：丢弃积压旧帧只处理最新。"""
        while True:
            item = self.ocr_queue.get()
            if item is None:
                break
            while True:
                try:
                    item = self.ocr_queue.get_nowait()
                except queue.Empty:
                    break
            frame, origin, gen = item
            try:
                engine = self._get_ocr()
                if self.ctx.overlay_mode == "inplace":
                    # 覆盖模式：带坐标识别 + 归组（保证换行长句的翻译上下文完整）
                    items = engine.extract_detail(frame)
                    blocks = group_lines(items, frame=frame)
                    blocks = [b for b in blocks if not is_own_ui_text(b.get("text", ""))]
                    ox, oy = origin
                    for b in blocks:
                        bx = b["box"]
                        b["box"] = [bx[0] + ox, bx[1] + oy, bx[2] + ox, bx[3] + oy]
                    logging.info("OCR 完成：%d 行合并为 %d 个文本块", len(items), len(blocks))
                    if blocks:
                        self.ctx.bridge.push("stage", {"text": "⟳ 正在翻译…", "tone": "warn"})
                        self.translate_queue.put(("positioned", blocks, gen))
                else:
                    text = engine.extract(frame)
                    if text and not is_own_ui_text(text):
                        self.ctx.bridge.push("stage", {"text": "⟳ 正在翻译…", "tone": "warn"})
                        self.translate_queue.put(("panel", text, gen))
            except Exception:
                logging.error("OCR 处理失败:\n%s", traceback.format_exc())
                self.ctx.status("OCR 处理失败（见日志）")

    @staticmethod
    def _boxes_overlap(a, b):
        return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])

    def _translate_worker(self):
        """翻译线程：与 OCR 并行（OCR 占 CPU、翻译走网络，互不抢占）。"""
        while True:
            item = self.translate_queue.get()
            if item is None:
                break
            kind, payload, gen = item
            if gen != self._gen:
                continue  # 已作废的旧任务
            try:
                if kind == "positioned":
                    self._handle_positioned(payload, gen)
                else:
                    translated = self.ctx.translator.translate(payload)
                    if translated is not None and gen == self._gen:
                        self.ctx.bridge.push("stage", {"text": "✓ 翻译完成", "tone": "success"})
                        self.ctx.ui_queue.put(("translation", payload, translated))
            except Exception:
                logging.error("翻译处理失败:\n%s", traceback.format_exc())
                self.ctx.status("翻译处理失败（见日志）")

    def _handle_positioned(self, payload, gen):
        lines = [it["text"] for it in payload]
        key = "\n".join(lines)
        if key == self._last_key:
            return
        self._last_key = key
        translated = self.ctx.translator.translate_lines(lines)
        if gen != self._gen:
            return  # 翻译期间发生重置：结果作废，防止旧译文跟着回来
        fx, fy = self.ctx.dpi_factors
        now_t = time.time()
        positioned = [
            {
                "box": [int(it["box"][0] * fx), int(it["box"][1] * fy),
                        int(it["box"][2] * fx), int(it["box"][3] * fy)],
                "text": t,
                "t": now_t,  # 出生时间（TTL 清理用）
            }
            for it, t in zip(payload, translated) if t
        ]
        if not positioned:
            return
        old = self.positioned_history
        # 场景切换兜底：新块多且与旧块完全无重叠 → 画面内容已整体更换，旧块全丢
        if old and len(positioned) >= 3 and not any(
                self._boxes_overlap(o["box"], n["box"]) for o in old for n in positioned):
            old = []
            logging.info("检测到场景切换：丢弃全部旧译文块")
        # 跨帧累积：新块覆盖重叠旧块，其余保留；TTL 过期块移除
        kept = [
            b for b in old
            if not any(self._boxes_overlap(b["box"], n["box"]) for n in positioned)
            and now_t - b.get("t", now_t) < BLOCK_TTL_S
        ]
        self.positioned_history = (kept + positioned)[-MAX_BLOCKS:]
        logging.info("翻译完成：%d 块（累计 %d 块）", len(positioned), len(self.positioned_history))
        self.ctx.bridge.push("stage", {"text": f"✓ 翻译完成（{len(positioned)} 块）", "tone": "success"})
        self.ctx.schedule_stage_revert()
        if self.ctx.overlay_mode == "inplace":
            self.ctx.ui_queue.put(("positioned", list(self.positioned_history)))
        # 通知订阅方（App：翻译历史 + 歌词）
        self.ctx.on_translation(positioned, lines, translated)

    def _expiry_loop(self):
        """TTL 兜底：过期块自动移除（场景切换漏检时，即使没有新翻译，残留也会消失）。"""
        while True:
            time.sleep(1.0)
            if not self.positioned_history:
                continue
            now_t = time.time()
            alive = [b for b in self.positioned_history if now_t - b.get("t", now_t) < BLOCK_TTL_S]
            if len(alive) != len(self.positioned_history):
                self.positioned_history = alive
                if self.ctx.overlay_mode == "inplace" and not self.ctx.overlay_hidden:
                    self.ctx.ui_queue.put(("positioned", list(alive)))

    def _preload_ocr(self):
        """启动时后台预加载模型：用户切去游戏的空档完成加载。"""
        try:
            logging.info("开始预加载 PaddleOCR 模型…")
            self._get_ocr()
            logging.info("PaddleOCR 模型加载完成")
        except Exception:
            logging.error("OCR 模型预加载失败:\n%s", traceback.format_exc())
