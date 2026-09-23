"""
游戏实时翻译工具
链路：游戏窗口/区域监控（帧差检测+变化区域裁剪）→ PaddleOCR（面板归组）
    → LLM 翻译（逐行并发）→ 置顶覆盖层显示；F5 截图直译（视觉模型）与问 AI 走 Web UI
UI：pywebview（HTML/JS 控制面板，webui.py 桥接）+ tkinter（覆盖层，需 Win32 透明/穿透特性）

热键：F5 截图直译 | F6 立即翻译 | F7 捕获游戏窗口 | F8 框选区域 | F9 暂停/恢复 | F11 覆盖/面板
"""
import sys
import os
import logging
import threading
import queue
import time
import traceback

# 日志写入文件，便于排查问题（打包后放在 exe 旁边）
if getattr(sys, "frozen", False):
    LOG_FILE = os.path.join(os.path.dirname(sys.executable), "game-translator.log")
else:
    LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game-translator.log")
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)

# Windows 控制台中文输出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import tkinter as tk

# 关键：开启进程 DPI 感知，让 tkinter 使用物理像素坐标，
# 与截屏/OCR 的物理像素坐标一致（否则 125%/150% 缩放下覆盖位置整体偏移）
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from app.config import Config
from app.capture import RegionMonitor
from app.ocr_engine import OCREngine
from app.translator import Translator
from app.overlay import OverlayWindow
from app.region_select import select_region
from app.hotkeys import HotkeyManager, VK_F5, VK_F6, VK_F7, VK_F8, VK_F9, VK_F11, VK_LWIN, VK_RWIN
from app.textblock import group_lines
from app import vision
from webui import EventBridge, PyApi

# 本工具自身 UI 的文案词表（OCR 兜底过滤：识别结果若全由这些词组成，说明截到了自己的面板，丢弃）
_UI_WORDS = (
    "f5", "f6", "f7", "f8", "f9", "f10", "f11",
    "截图直译", "立即翻译", "游戏窗口", "框选区域", "选区", "窗口",
    "暂停", "恢复", "后端", "显示", "覆盖原文", "独立面板",
    "监控中", "已暂停", "等待选择区域", "捕获游戏窗口",
    "正在识别", "正在翻译", "翻译完成", "截图发送中",
    "视觉模型", "翻译中", "思考过程", "译文",
    "按阅读顺序", "整屏发给", "含思考过程",
    "llm", "deepl", "ollama",
    "f6立即翻译", "f7窗口", "f8选区", "f9暂停", "f10后端", "f11显示",
    "游戏实时翻译", "输出历史", "提问", "带画面", "问", "未监控", "显示切换", "游戏",
)


def _is_own_ui_text(text):
    """识别文本是否来自本工具自身 UI（截图混入面板时的终极兜底）。
    规则：规范化后能被 UI 词表完全拆解 → 是自身文案。"""
    import re
    norm = re.sub(r"[\s()\[\]{}:：|·，,。.\-●○✓⟳⏸]+", "", text).lower()
    if not norm or len(norm) > 60:  # 超长 text 不可能是纯 UI 文案拼接
        return False
    changed = True
    while changed and norm:
        changed = False
        for w in _UI_WORDS:
            if w in norm:
                norm = norm.replace(w, "", 1)
                changed = True
    return norm == ""


def _thumb_b64(frame, width=520, quality=80):
    """截图帧 → JPEG base64 data URI（推送给 Web UI 历史卡片显示）"""
    try:
        import base64
        import cv2
        import numpy as np
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, int(h * width / w)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(buf).decode("ascii")
    except Exception:
        return None


class App:
    window_ids = []  # 本工具所有窗口的 winfo_id（用于 F7 排除自身窗口）

    def __init__(self, bridge: EventBridge, wins_provider=None):
        App.window_ids = []
        self._bridge = bridge
        self._status_text = ""
        self._wins_provider = wins_provider or (lambda: {})  # () -> {name: pywebview window}
        self._webview_hwnd = 0            # 主控制窗句柄（截图涂黑排除/F7 排除用）
        self._output_open = False         # 输出面板窗口显示态
        self.config = Config()
        self.translator = Translator(self.config)

        # 进程降为低优先级：游戏优先占用 CPU，翻译工具不再抢资源导致卡顿
        try:
            import psutil
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass

        self.ocr_engine = None          # PaddleOCR 首次使用时才初始化（启动快）
        self.monitor = None
        # 自动翻译开关（F7 进入游戏窗口时自动关闭，F9 切换，状态跨重启保存）
        self.paused = not self.config.get("auto_translate", default=True)
        self._last_positioned_key = None  # 覆盖模式去重（与上一帧相同的文本集合不重复翻译）
        self._region_offset = (0, 0)      # 监控区域屏幕偏移（start_monitor 时更新）
        self._game_hwnd = None            # F7 捕获的游戏窗口句柄（Win 键智能屏蔽用）
        self._positioned_history = []     # 覆盖层译文累积（跨帧保留未变化区域的旧译文）

        # 队列：监控线程 -> OCR 线程 -> 翻译线程 -> UI（流水线并发）
        self.ocr_queue = queue.Queue()
        self.translate_queue = queue.Queue()
        self.ui_queue = queue.Queue()  # 仅 tk 侧消费（覆盖层绘制/窗口关闭）
        self._ocr_lock = threading.Lock()

        # ---------- tkinter 侧：隐藏主窗（仅作 overlay 载体与 DPI 校准） ----------
        self.root = tk.Tk()
        self.root.title("游戏实时翻译")
        self.root.withdraw()  # 控制面板已由 pywebview 承担，tk 主窗隐藏
        # pythonw 模式下 tk 的 withdraw 偶发不生效（窗口仍可见/占任务栏）：
        # Win32 级 SW_HIDE 双保险
        try:
            import ctypes
            tk_hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            ctypes.windll.user32.ShowWindow(tk_hwnd, 0)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.overlay_mode = self.config.get("overlay_mode", default="inplace")

        # ---- 屏幕坐标系校准 ----
        self.root.update_idletasks()
        import mss as _mss
        _MSS = getattr(_mss, "MSS", _mss.mss)
        with _MSS() as _sct:
            _mon = _sct.monitors[1]
        self._dpi_fx = self.root.winfo_screenwidth() / _mon["width"]
        self._dpi_fy = self.root.winfo_screenheight() / _mon["height"]
        logging.info(
            "屏幕校准: tkinter=%dx%d, mss=%dx%d, 系数=(%.3f, %.3f)",
            self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
            _mon["width"], _mon["height"], self._dpi_fx, self._dpi_fy,
        )

        self.overlay = OverlayWindow(self.root, self.config, mode=self.overlay_mode)
        App.window_ids.append(self.root.winfo_id())
        App.window_ids.append(self.overlay.win.winfo_id())
        # webview 控制面板窗口（Win32 级别也加入排除列表，避免被 F7 当成游戏目标/截图混入）
        try:
            import webview
            # pywebview 窗口句柄在窗口创建后才能拿到，由 webui 侧回填（见 main()）
        except Exception:
            pass

        # OCR 线程 + 翻译线程（并行流水线），启动即后台预加载模型
        threading.Thread(target=self._ocr_worker, daemon=True).start()
        threading.Thread(target=self._translate_worker, daemon=True).start()
        threading.Thread(target=self._preload_ocr, daemon=True).start()
        # 主面板置顶保持
        threading.Thread(target=self._keep_on_top_loop, daemon=True).start()
        self.root.after(100, self._poll_ui_queue)

    def _preload_ocr(self):
        """启动时后台预加载 OCR 模型：用户切去游戏的空档完成加载，首次翻译不再等"""
        with self._ocr_lock:
            if self.ocr_engine is not None:
                return
            try:
                logging.info("开始预加载 PaddleOCR 模型…")
                self.ocr_engine = OCREngine(
                    lang="en",
                    min_score=self.config.get("ocr_min_score", default=0.6),
                    high_accuracy=self.config.get("ocr_high_accuracy", default=False),
                )
                logging.info("PaddleOCR 模型加载完成")
            except Exception:
                logging.error("OCR 模型预加载失败:\n%s", traceback.format_exc())

    # ---------- Web UI 推送 ----------

    def _status(self, text):
        """详细状态行（显示在 Web UI 状态卡下方）"""
        self._status_text = text
        self._push_state()

    def _push_state(self):
        self._bridge.push("state", {
            "paused": self.paused,
            "overlay_mode": self.overlay_mode,
            "region": list(self.config.region) if self.config.region and self.config.region != "fullscreen" else self.config.region,
            "status": self._status_text,
        })

    def _set_stage(self, text, tone="info", revert_to=None, revert_ms=2000):
        """大字状态（翻译流程实时提示）；revert_to 给定时自动回落"""
        self._bridge.push("stage", {"text": text, "tone": tone})
        old = getattr(self, "_stage_timer", None)
        if old:
            old.cancel()
        if revert_to:
            def _revert():
                self._bridge.push("stage", {"text": revert_to, "tone": "info"})
            t = threading.Timer(revert_ms / 1000, _revert)
            t.daemon = True
            t.start()
            self._stage_timer = t

    def _monitor_status_text(self):
        if self.paused:
            return "⏸ 已暂停"
        if not self.config.region:
            return "等待选择区域\n按 F7 捕获游戏窗口"
        return "● 监控中"

    # ---------- 截图排除（涂黑自身窗口矩形） ----------

    def _own_window_rects(self):
        """本工具自身不透明窗口的屏幕物理矩形列表（截图涂黑排除用）。
        纯 Win32 查询，线程安全；跳过全屏矩形（inplace 覆盖层是透明画布，不遮挡游戏）。"""
        import ctypes
        import ctypes.wintypes as wintypes
        user32 = ctypes.windll.user32
        GA_ROOT = 2
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        rects = []
        for wid in set(App.window_ids):
            try:
                hwnd = user32.GetAncestor(wid, GA_ROOT) or wid
                if not hwnd or not user32.IsWindowVisible(hwnd):
                    continue
                rect = wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    continue
                rw, rh = rect.right - rect.left, rect.bottom - rect.top
                if rw <= 0 or rh <= 0:
                    continue
                if rw >= sw and rh >= sh:  # inplace 全屏透明画布：跳过
                    continue
                rects.append((rect.left, rect.top, rw, rh))
            except Exception:
                pass
        # webview 控制面板窗口（主窗口隐藏，webview 是实际可见窗口）
        hwnd = getattr(self, "_webview_hwnd", None)
        if hwnd:
            try:
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    if rect.right > rect.left and rect.bottom > rect.top:
                        rects.append((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))
            except Exception:
                pass
        return rects

    # ---------- 监控与翻译链路 ----------

    def start_monitor(self):
        self.stop_monitor()
        region = self.config.region
        if not region:
            return
        self.monitor = RegionMonitor(
            region=region,
            interval_ms=self.config.get("capture_interval_ms", default=400),
            diff_threshold=self.config.get("diff_threshold", default=4.0),
            stable_ms=self.config.get("stable_ms", default=500),
            on_stable=self._on_stable_frame,
            force_ocr_ms=self.config.get("force_ocr_ms", default=2500),
            trigger_mode=self.config.get("trigger_mode", default="diff"),
            input_idle_ms=self.config.get("input_idle_ms", default=3000),
            min_interval_ms=self.config.get("min_translate_interval_ms", default=5000),
            game_hwnd=self._game_hwnd or 0,
            exclude_rects_provider=self._own_window_rects,
        )
        self._region_offset = self.monitor.offset
        self.monitor.start()
        if self.paused:
            self.monitor.pause()

    def stop_monitor(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor = None

    def _on_stable_frame(self, frame, origin, force=False):
        """监控线程回调：画面稳定，交给 OCR 队列（frame 为变化区域裁剪，origin 为其屏幕坐标）。
        force=True 为 F6 手动触发，暂停状态下依然执行。"""
        if not self.paused or force:
            if force:
                # 手动翻译 = 翻译当前画面：清空旧译文（避免场景切换后残留），
                # 立即刷新覆盖层（识别为空时也能清掉残留），重置去重键允许重译相同文本
                self._positioned_history = []
                self._last_positioned_key = None
                self.ui_queue.put(("positioned", []))
            self._bridge.push("stage", {"text": "⟳ 正在识别…", "tone": "warn"})
            self.ocr_queue.put((frame, origin))

    def _ocr_worker(self):
        """OCR 线程：丢弃积压旧帧只处理最新，识别结果交给翻译线程（流水线并发）"""
        while True:
            item = self.ocr_queue.get()
            if item is None:
                break
            while True:
                try:
                    item = self.ocr_queue.get_nowait()
                except queue.Empty:
                    break
            frame, origin = item
            try:
                with self._ocr_lock:
                    if self.ocr_engine is None:
                        self._status("正在加载 PaddleOCR 模型（首次约 10-30 秒）…")
                        self.ocr_engine = OCREngine(
                            lang="en",
                            min_score=self.config.get("ocr_min_score", default=0.6),
                            high_accuracy=self.config.get("ocr_high_accuracy", default=False),
                        )
                        self._status(f"监控中 · {self._region_text(self.config.region)}")

                if self.overlay_mode == "inplace":
                    # 覆盖模式：带坐标识别 + 按文本框面板归组（保证换行长句的翻译上下文完整）
                    items = self.ocr_engine.extract_detail(frame)
                    blocks = group_lines(items, frame=frame)
                    # 兜底过滤：剔除误截到的本工具自身 UI 文案（按钮/状态文字）
                    blocks = [b for b in blocks if not _is_own_ui_text(b.get("text", ""))]
                    ox, oy = origin
                    for b in blocks:
                        bx = b["box"]
                        b["box"] = [bx[0] + ox, bx[1] + oy, bx[2] + ox, bx[3] + oy]
                    logging.info("OCR 完成：%d 行合并为 %d 个文本块", len(items), len(blocks))
                    if blocks:
                        self._bridge.push("stage", {"text": "⟳ 正在翻译…", "tone": "warn"})
                        self.translate_queue.put(("positioned", blocks))
                else:
                    text = self.ocr_engine.extract(frame)
                    if text and not _is_own_ui_text(text):
                        self._bridge.push("stage", {"text": "⟳ 正在翻译…", "tone": "warn"})
                        self.translate_queue.put(("panel", text))
            except Exception as e:
                logging.error("OCR 处理失败:\n%s", traceback.format_exc())
                self._status(f"处理失败: {e}")

    @staticmethod
    def _boxes_overlap(a, b):
        """两个 box [x1,y1,x2,y2] 是否有重叠"""
        return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])

    def _translate_worker(self):
        """翻译线程：与 OCR 并行（OCR 占 CPU、翻译走网络，互不抢占）"""
        while True:
            item = self.translate_queue.get()
            if item is None:
                break
            kind, payload = item
            try:
                if kind == "positioned":
                    lines = [it["text"] for it in payload]
                    key = "\n".join(lines)
                    if key == self._last_positioned_key:
                        continue
                    self._last_positioned_key = key
                    translated = self.translator.translate_lines(lines)
                    fx, fy = self._dpi_fx, self._dpi_fy  # 物理 → tkinter 画布坐标
                    positioned = [
                        {
                            "box": [
                                int(it["box"][0] * fx),
                                int(it["box"][1] * fy),
                                int(it["box"][2] * fx),
                                int(it["box"][3] * fy),
                            ],
                            "text": t,
                        }
                        for it, t in zip(payload, translated) if t
                    ]
                    if positioned:
                        # 跨帧累积：新块覆盖与之重叠的旧块，其余旧译文保留
                        kept = [
                            old for old in self._positioned_history
                            if not any(self._boxes_overlap(old["box"], n["box"]) for n in positioned)
                        ]
                        self._positioned_history = kept + positioned
                        if len(self._positioned_history) > 30:
                            self._positioned_history = self._positioned_history[-30:]
                        logging.info("翻译完成：%d 块（累计 %d 块）", len(positioned), len(self._positioned_history))
                        self._bridge.push("stage", {
                            "text": f"✓ 翻译完成（{len(positioned)} 块）", "tone": "success",
                        })
                        threading.Timer(2.0, lambda: self._bridge.push(
                            "stage", {"text": self._monitor_status_text(), "tone": "info"})).start()
                        self.ui_queue.put(("positioned", list(self._positioned_history)))
                        # 翻译历史：原文/译文对照推给 Web UI（文本游戏回看）
                        self._bridge.push("translation", {
                            "original": "\n".join(lines),
                            "translated": "\n".join(t for t in translated if t),
                        })
                else:
                    translated = self.translator.translate(payload)
                    if translated is not None:
                        self._bridge.push("stage", {"text": "✓ 翻译完成", "tone": "success"})
                        self.ui_queue.put(("translation", payload, translated))
            except Exception as e:
                logging.error("翻译处理失败:\n%s", traceback.format_exc())
                self._status(f"处理失败: {e}")

    # ---------- UI 队列消费（tk 线程：仅覆盖层相关） ----------

    def _poll_ui_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "positioned":
                    self.overlay.update_positioned(item[1])
                elif kind == "translation":
                    _, original, translated = item
                    self.overlay.update_translation(translated, self.translator.backend, self.paused)
                elif kind == "shutdown":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    # ---------- 热键/按钮动作（线程安全，可从热键线程或 webview 线程调用） ----------

    def select_region_async(self):
        self.root.after(0, self.do_select_region)

    def set_fullscreen_async(self):
        self.root.after(0, self.do_capture_foreground)

    def trigger_now_async(self):
        if self.monitor:
            self.monitor.capture_now()

    def vision_translate_async(self):
        """截图直译（F5）：整屏截图直接发给视觉模型，流式输出到 Web UI"""
        if self.monitor:
            threading.Thread(target=self._vision_worker, daemon=True).start()

    def ask_ai_async(self, question, with_image=False):
        """问 AI（Web UI 输入框触发）"""
        question = (question or "").strip()
        if question:
            threading.Thread(target=self._ask_worker, args=(question, with_image), daemon=True).start()

    def _ask_worker(self, question, with_image=False, image_b64=None):
        try:
            thumb = None
            frame = None
            if image_b64:
                # 引用历史截图提问：直接用该图，无需重新截图
                thumb = image_b64
            elif with_image and self.monitor:
                self._bridge.push("stage", {"text": "⟳ 截图发送中…", "tone": "warn"})
                frame = self.monitor.clean_grab()
                if frame is not None:
                    thumb = _thumb_b64(frame)

            self._bridge.push("stage", {"text": "⟳ AI 回答中…", "tone": "warn"})
            self._bridge.push("ask_start", {"question": question, "thumb": thumb})

            for kind, chunk in vision.ask_stream(
                question, frame, self.config,
                with_image=with_image or bool(image_b64), image_b64=image_b64,
            ):
                self._bridge.push("vision_delta", {"type": kind, "text": chunk})

            self._bridge.push("vision_done")
            self._bridge.push("stage", {"text": "✓ 回答完成", "tone": "success"})
            logging.info("问 AI 完成（%s）", "引用截图" if image_b64 else ("带画面" if with_image else "纯文本"))
        except Exception as e:
            logging.error("问 AI 失败:\n%s", traceback.format_exc())
            self._bridge.push("vision_delta", {"type": "content", "text": f"\n[问 AI 失败: {e}]"})
            self._bridge.push("vision_done")

    def _vision_worker(self):
        try:
            self._bridge.push("stage", {"text": "⟳ 截图发送中…", "tone": "warn"})
            frame = self.monitor.clean_grab()
            if frame is None:
                self._bridge.push("vision_delta", "[截图失败]")
                self._bridge.push("vision_done")
                return

            thumb = _thumb_b64(frame)
            self._bridge.push("stage", {"text": "⟳ 视觉模型翻译中…", "tone": "warn"})
            self._bridge.push("vision_start", {"thumb": thumb})
            logging.info("截图直译：发送 %dx%d 给视觉模型（流式）", frame.shape[1], frame.shape[0])

            for kind, chunk in vision.translate_screenshot_stream(frame, self.config):
                self._bridge.push("vision_delta", {"type": kind, "text": chunk})

            self._bridge.push("vision_done")
            self._bridge.push("stage", {"text": "✓ 翻译完成", "tone": "success"})
            logging.info("截图直译完成（流式）")
        except Exception as e:
            logging.error("截图直译失败:\n%s", traceback.format_exc())
            self._bridge.push("vision_delta", {"type": "content", "text": f"\n[截图直译失败: {e}]"})
            self._bridge.push("vision_done")

    def _get_foreground_rect(self):
        """获取前台窗口客户区的屏幕物理坐标 (x, y, w, h)，并记录游戏窗口句柄。
        无法获取或目标是自己时返回 None"""
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        # 排除本工具自己的窗口（覆盖层/webview 面板）
        GA_ROOT = 2
        my_windows = set()
        for wid in App.window_ids:
            try:
                my_windows.add(user32.GetAncestor(user32.GetParent(wid), GA_ROOT))
                my_windows.add(user32.GetAncestor(wid, GA_ROOT))
            except Exception:
                pass
        wh = getattr(self, "_webview_hwnd", None)
        if wh:
            my_windows.add(wh)
        if hwnd in my_windows:
            return None

        rect = ctypes.wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        pt = ctypes.wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 100 or h < 100:
            return None
        self._game_hwnd = hwnd  # 记住游戏窗口，用于 Win 键智能屏蔽
        return (pt.x, pt.y, w, h)

    def do_capture_foreground(self):
        """游戏窗口模式：捕获前台窗口客户区（自动排除桌面/任务栏），仅翻译游戏内容。
        进入游戏窗口时默认关闭自动翻译（F6/按钮手动触发，F9 恢复自动）。"""
        region = self._get_foreground_rect()
        if not region:
            self._status("请先点击游戏窗口，再按 F7（工具自身窗口会被排除）")
            return
        self.config.region = region
        # 进入游戏窗口：默认关闭自动翻译，仅手动触发（F6/按钮），F9 可恢复
        self.config.set(False, "auto_translate")
        self.config.save()
        self.paused = True
        self._status(f"监控中 · 游戏窗口 {region}（自动翻译已关闭）")
        self._positioned_history = []  # 换了监控区域，清空旧译文
        self._set_stage("⏸ 自动翻译已关闭\nF6/F5 手动 · F9 恢复自动")
        self.start_monitor()
        if self.overlay_mode != "inplace":
            self._do_toggle_overlay_mode()
        else:
            self.overlay.update_status(self.translator.backend, self.paused)
        self._push_state()

    def _on_win_key(self):
        """Win 键智能屏蔽：游戏窗口在前台时吞掉（防游戏误触），
        其他情况把按键转发回系统，开始菜单正常弹出"""
        import ctypes
        user32 = ctypes.windll.user32
        if self._game_hwnd and user32.GetForegroundWindow() == self._game_hwnd:
            return  # 游戏前台：吞掉
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_LWIN, 0, 0, 0)
        user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)

    @staticmethod
    def _region_text(region):
        return "全屏" if region == "fullscreen" else str(region)

    def do_select_region(self):
        region = select_region(self.root)
        if region:
            # 框选坐标是 tkinter 坐标系，统一换算为物理像素存储
            region_phys = (
                int(region[0] / self._dpi_fx),
                int(region[1] / self._dpi_fy),
                int(region[2] / self._dpi_fx),
                int(region[3] / self._dpi_fy),
            )
            self.config.region = region_phys
            self._game_hwnd = None  # 框选区域与游戏窗口不再对应，回退屏幕截图模式
            self._status(f"监控中 · 区域 {region_phys}")
            self._set_stage("● 监控中")
            self.start_monitor()
            self.overlay.update_status(self.translator.backend, self.paused)
            self._push_state()

    def toggle_pause(self):
        self.root.after(0, self._do_toggle_pause)

    def _do_toggle_pause(self):
        self.paused = not self.paused
        if self.monitor:
            if self.paused:
                self.monitor.pause()
            else:
                self.monitor.resume()
        self.config.set(not self.paused, "auto_translate")  # 状态跨重启保存
        self.config.save()
        self._set_stage(self._monitor_status_text())
        self.overlay.update_status(self.translator.backend, self.paused)
        self._push_state()

    # ---------- 悬浮窗模式切换（F11） ----------

    def toggle_overlay_mode(self):
        self.root.after(0, self._do_toggle_overlay_mode)

    def _do_toggle_overlay_mode(self):
        self.overlay_mode = "panel" if self.overlay_mode == "inplace" else "inplace"
        self.config.set(self.overlay_mode, "overlay_mode")
        self.config.save()
        try:
            self.overlay.win.destroy()
        except Exception:
            pass
        self.overlay = OverlayWindow(self.root, self.config, mode=self.overlay_mode)
        App.window_ids.append(self.overlay.win.winfo_id())
        self.overlay.update_status(self.translator.backend, self.paused)
        self._last_positioned_key = None  # 切换后强制重新翻译一次
        self._positioned_history = []
        self._push_state()

    # ---------- 输出面板窗口（第二窗口，固定尺寸，显示/隐藏切换） ----------

    def _toggle_output_window(self, force_hide=False):
        """显示/隐藏输出面板窗口。窗口常驻（隐藏保活），Vue 状态与历史记录不丢失。"""
        wins = self._wins_provider()
        if not wins:
            return
        win = wins.get("panel")
        if not win:
            return
        try:
            if force_hide or getattr(self, "_output_open", False):
                win.hide()
                self._output_open = False
            else:
                win.show()
                self._output_open = True
            logging.info("输出面板%s", "显示" if self._output_open else "隐藏")
            self._bridge.push("output_state", {"open": self._output_open}, target="main")
        except Exception:
            logging.error("输出面板切换失败:\n%s", traceback.format_exc())

    def _keep_on_top_loop(self):
        """每 5 秒用 Win32 直设置顶（HWND_TOPMOST）。不用 pywebview 的 on_top：
        其内部走 WinForms 属性跨线程赋值不可靠（异常被吞，置顶从未生效）。"""
        import ctypes
        user32 = ctypes.windll.user32
        HWND_TOPMOST = -1
        SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
        while True:
            time.sleep(5)
            try:
                hwnds = [self._webview_hwnd]
                # 输出面板窗（可见时才需要置顶）
                panel_hwnd = _find_webview_hwnd("输出面板")
                if panel_hwnd:
                    hwnds.append(panel_hwnd)
                for hwnd in hwnds:
                    if hwnd and user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd):
                        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
            except Exception:
                logging.error("置顶循环异常:\n%s", traceback.format_exc())

    # ---------- 生命周期 ----------

    def on_close(self):
        """主窗关闭（✕/WM_CLOSE/Alt+F4）。清理后进程级退出：
        webview.start() 因隐藏的输出窗永不返回，destroy 隐藏窗口又会死锁，
        因此所有退出路径统一 os._exit。"""
        try:
            self.stop_monitor()
            if getattr(self, "_hotkeys", None):
                self._hotkeys.stop()
            self.config.save()
        except Exception:
            pass
        self.ui_queue.put(("shutdown",))
        os._exit(0)

    def run(self):
        # 已有保存区域（含全屏模式）则直接开始监控
        if self.config.region:
            self._status(f"监控中 · {self._region_text(self.config.region)}")
            self.start_monitor()
        self.overlay.update_status(self.translator.backend, self.paused)
        self._push_state()

        hotkey_bindings = {
            VK_F5: self.vision_translate_async,
            VK_F6: self.trigger_now_async,
            VK_F7: self.set_fullscreen_async,
            VK_F8: self.select_region_async,
            VK_F9: self.toggle_pause,
            VK_F11: self.toggle_overlay_mode,
        }
        # 智能 Win 键屏蔽：仅游戏窗口在前台时吞掉，其他情况转发（开始菜单正常）
        if self.config.get("disable_win_key", default=False):
            hotkey_bindings[VK_LWIN] = self._on_win_key
            hotkey_bindings[VK_RWIN] = self._on_win_key
            logging.info("Win 键智能屏蔽已启用（仅游戏前台时拦截）")
        self._hotkeys = HotkeyManager(hotkey_bindings)
        self._hotkeys.start()

        self.root.mainloop()


def _find_webview_hwnd(title="游戏实时翻译"):
    """枚举顶层窗口，按标题查找 pywebview（WebView2）窗口句柄"""
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    result = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    @WNDENUMPROC
    def callback(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value == title and user32.IsWindowVisible(hwnd):
                result.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return result[0] if result else None


def main():
    import webview

    bridge = EventBridge()
    holder = {}
    wins = {}  # name -> pywebview window
    ready = threading.Event()

    def tk_main():
        # tkinter 在子线程内创建并 mainloop（覆盖层载体）
        app = App(bridge, wins_provider=lambda: wins)
        holder["app"] = app
        ready.set()
        try:
            app.run()
        except Exception:
            logging.error("tk 侧异常:\n%s", traceback.format_exc())

    threading.Thread(target=tk_main, daemon=True).start()
    ready.wait(timeout=15)
    app = holder.get("app")
    if app is None:
        raise RuntimeError("tk 侧初始化失败")

    ui_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
    api = PyApi(lambda: holder["app"], wins=wins)

    # 主控制窗（固定尺寸 348x620，无边框，永不做 resize）
    win_main = webview.create_window(
        "游戏实时翻译", os.path.join(ui_dir, "index.html"), js_api=api,
        x=80, y=100, width=348, height=620, frameless=True, on_top=True,
        background_color="#14141f",
    )
    # 输出面板窗（固定尺寸 860x640，隐藏启动，Vue 双 tab）
    win_panel = webview.create_window(
        "输出面板", os.path.join(ui_dir, "panel.html"), js_api=api,
        x=444, y=100, width=860, height=640, frameless=True, hidden=True, on_top=True,
        background_color="#14141f",
    )
    wins["main"] = win_main
    wins["panel"] = win_panel

    win_main.events.closed += app.on_close

    def _on_main_shown():
        hwnd = _find_webview_hwnd()
        app._webview_hwnd = hwnd or 0
        logging.info("主控制窗句柄: %s", hwnd or "未找到！")

    win_main.events.shown += _on_main_shown
    bridge.attach("main", win_main)
    bridge.attach("panel", win_panel)
    webview.start()  # 主线程消息循环（阻塞至主窗口关闭）

    # webview 关闭后让 tk 侧退出
    app.ui_queue.put(("shutdown",))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # 打包后自测：验证 paddle GPU / OCR 引擎 / 凭据管理器可用，结果写日志
        logging.info("=== 自测开始 ===")
        try:
            import paddle
            logging.info("paddle %s, CUDA: %s", paddle.__version__, paddle.device.is_compiled_with_cuda())
        except Exception:
            logging.error("paddle 导入失败:\n%s", traceback.format_exc())
        try:
            from app.ocr_engine import OCREngine
            OCREngine(lang="en", min_score=0.6, high_accuracy=False)
            logging.info("OCR 引擎初始化成功")
        except Exception:
            logging.error("OCR 引擎初始化失败:\n%s", traceback.format_exc())
        try:
            from app import secrets
            # 用独立槽位测试，绝不触碰 llm 槽位的真实密钥
            secrets.set_api_key("selftest-ok", section="selftest")
            ok = secrets.get_api_key(section="selftest") == "selftest-ok"
            secrets.delete_api_key(section="selftest")
            logging.info("凭据管理器: %s", "可用" if ok else "不可用")
        except Exception:
            logging.error("凭据管理器失败:\n%s", traceback.format_exc())
        logging.info("=== 自测结束，详见同目录 game-translator.log ===")
    else:
        main()
