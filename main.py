"""
游戏实时翻译工具

架构：
  UI     pywebview 双窗口（主控窗 + Vue 输出面板），webui.py 提供 JS 桥
         tkinter 仅保留覆盖层（Win32 透明/穿透特性所必需）
  链路   区域监控（帧差检测）→ PaddleOCR → LLM 翻译 → 覆盖层/输出面板
  窗口   app/windows.py 集中全部 Win32 窗口操作（透明度/置顶/形态/截图排除）

热键：F5 截图直译 | F6 立即翻译 | F7 捕获游戏窗口 | F8 框选区域
      F9 暂停/恢复自动翻译 | F11 覆盖原文/独立面板
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
from app.uifilter import is_own_ui_text
from app.windows import WindowManager, find_hwnd, TITLE_MAIN, TITLE_PANEL
from app import vision
from webui import EventBridge, PyApi


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
        # Win32 窗口操作全部委托 WindowManager（透明度/置顶/形态/截图排除）
        self.wm = WindowManager(self._wins_provider, lambda: App.window_ids)
        self._output_open = False         # 输出面板窗口显示态
        self._overlay_hidden = False      # 覆盖层译文显示开关（输出面板工具栏控制）
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

        # lyrics 现在是输出面板的 tab（不再是独立显示模式），历史配置归一到 inplace
        mode = self.config.get("overlay_mode", default="inplace")
        self.overlay_mode = "inplace" if mode == "lyrics" else mode

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
        # 窗口置顶保持（委托 WindowManager）
        threading.Thread(target=self.wm.keep_on_top_loop, daemon=True).start()
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
            "overlay_hidden": self._overlay_hidden,
            "region": list(self.config.region) if self.config.region and self.config.region != "fullscreen" else self.config.region,
            "status": self._status_text,
        })

    def _toggle_overlay_visible(self):
        """显示/隐藏覆盖层译文（隐藏时清空覆盖层，恢复时重绘历史译文）"""
        self._overlay_hidden = not self._overlay_hidden
        if self._overlay_hidden:
            self.ui_queue.put(("positioned", []))  # 清空指令必须执行（poll 不拦截空列表）
        else:
            self.ui_queue.put(("positioned", list(self._positioned_history)))
        self._push_state()

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

    # ---------- 监控与翻译链路 ----------
    # 截图排除（涂黑自身窗口矩形）见 self.wm.own_window_rects（app/windows.py）

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
            exclude_rects_provider=self.wm.own_window_rects,
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
                    blocks = [b for b in blocks if not is_own_ui_text(b.get("text", ""))]
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
                    if text and not is_own_ui_text(text):
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
                        # 歌词（输出面板「歌词」tab）：面积最大的块 = 对话主文本
                        main_block = max(
                            positioned,
                            key=lambda p: (p["box"][2] - p["box"][0]) * (p["box"][3] - p["box"][1]),
                        )
                        self._bridge.push("lyrics", main_block["text"])
                        if self.overlay_mode == "inplace":
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
                    _, positioned = item
                    # 空列表=清空指令总是执行；非空且已隐藏时跳过（保持隐藏）
                    if positioned or not self._overlay_hidden:
                        self.overlay.update_positioned(positioned)
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

    def _stream_to_bridge(self, stream, thumb, question=None):
        """通用流式输出（直译/问答共用）：创建卡片 → 增量推送 → 完成标记"""
        if question is not None:
            self._bridge.push("ask_start", {"question": question, "thumb": thumb})
        else:
            self._bridge.push("vision_start", {"thumb": thumb})
        for kind, chunk in stream:
            self._bridge.push("vision_delta", {"type": kind, "text": chunk})
        self._bridge.push("vision_done")

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
            self._stream_to_bridge(
                vision.ask_stream(
                    question, frame, self.config,
                    with_image=with_image or bool(image_b64), image_b64=image_b64,
                ),
                thumb, question=question,
            )
            self._bridge.push("stage", {"text": "✓ 回答完成", "tone": "success"})
            logging.info("问 AI 完成（%s）", "引用截图" if image_b64 else ("带画面" if with_image else "纯文本"))
        except Exception as e:
            logging.error("问 AI 失败:\n%s", traceback.format_exc())
            self._bridge.push("vision_delta", {"type": "content", "text": f"\n[问 AI 失败: {e}]"})
            self._bridge.push("vision_done")

    def _grab_full_frame(self):
        """F5 专用抓屏：临时隐藏自身全部窗口，等 DWM 合成更新（250ms）后
        截完整游戏画面（无涂黑块、无工具 UI 混入），截完立即恢复窗口。"""
        import ctypes
        user32 = ctypes.windll.user32
        hidden = self.wm.visible_hwnds()
        try:
            for h in hidden:
                user32.ShowWindow(h, 0)  # SW_HIDE
            if hidden:
                time.sleep(0.25)  # 等 DWM 完成合成更新（30ms 不够，残影会被截到）
            return self.monitor.clean_grab()
        finally:
            for h in hidden:
                user32.ShowWindow(h, 5)  # SW_SHOW

    def _vision_worker(self):
        try:
            self._bridge.push("stage", {"text": "⟳ 截图发送中…", "tone": "warn"})
            frame = self._grab_full_frame()
            if frame is None:
                self._bridge.push("vision_delta", {"type": "content", "text": "[截图失败]"})
                self._bridge.push("vision_done")
                return

            self._bridge.push("stage", {"text": "⟳ 视觉模型翻译中…", "tone": "warn"})
            logging.info("截图直译：发送 %dx%d 给视觉模型（流式）", frame.shape[1], frame.shape[0])
            self._stream_to_bridge(
                vision.translate_screenshot_stream(frame, self.config),
                _thumb_b64(frame),
            )
            self._bridge.push("stage", {"text": "✓ 翻译完成", "tone": "success"})
            logging.info("截图直译完成（流式）")
        except Exception as e:
            logging.error("截图直译失败:\n%s", traceback.format_exc())
            self._bridge.push("vision_delta", {"type": "content", "text": f"\n[截图直译失败: {e}]"})
            self._bridge.push("vision_done")

    @staticmethod
    def _process_elevated(pid):
        """指定进程是否以管理员权限运行。无法判断返回 None。
        UIPI 规则：普通权限进程无法抓取管理员权限窗口（F7 失败的常见原因）"""
        import ctypes
        import ctypes.wintypes as wintypes
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcessToken.restype = wintypes.BOOL
        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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

    @classmethod
    def _self_elevated(cls):
        import ctypes
        return bool(cls._process_elevated(ctypes.windll.kernel32.GetCurrentProcessId()))

    def _get_foreground_rect(self, hwnd=None):
        """获取游戏窗口客户区的屏幕物理坐标 (x, y, w, h)，并记录游戏窗口句柄。
        hwnd=None 取前台窗口（F7）；指定 hwnd 用于「选择窗口」列表捕获。
        无法获取或目标是自己时返回 None"""
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        if hwnd is None:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                logging.warning("F7 诊断: GetForegroundWindow 返回空（无前台窗口？）")
                return None

        # ---- 诊断信息：窗口标题 / 类名 / PID / 权限（写入 exe 旁日志，排障用）----
        try:
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(max(n + 1, 64))
            user32.GetWindowTextW(hwnd, buf, n + 1)
            cls_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls_buf, 64)
            logging.info(
                "F7 诊断: 前台窗口 标题[%s] 类[%s] pid=%d 本工具管理员=%s 目标管理员=%s",
                buf.value, cls_buf.value, pid.value,
                self._self_elevated(), self._process_elevated(pid.value),
            )
        except Exception:
            logging.info("F7 诊断: 前台窗口信息获取失败", exc_info=True)

        # 排除本工具自己的窗口（tk 覆盖层 + webview 主控/输出面板）
        if hwnd in self.wm.exclude_hwnds():
            logging.info("F7 诊断: 前台是本工具自身窗口，已排除")
            return None

        rect = ctypes.wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            logging.warning("F7 诊断: GetClientRect 失败（窗口可能已关闭）")
            return None
        pt = ctypes.wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 100 or h < 100:
            logging.warning("F7 诊断: 客户区尺寸 %dx%d 过小，忽略", w, h)
            return None
        # UIPI 检测：目标窗口管理员权限而本工具普通权限 → 抓取会被系统静默拒绝
        if self._process_elevated(pid.value) and not self._self_elevated():
            logging.warning("F7 诊断: 游戏以管理员权限运行，本工具为普通权限，无法捕获窗口")
            self._status("游戏以管理员运行，本工具权限不足：请右键 GameTranslator.exe 以管理员身份运行")
            return None
        self._game_hwnd = hwnd  # 记住游戏窗口，用于 Win 键智能屏蔽
        return (pt.x, pt.y, w, h)

    def do_capture_foreground(self, hwnd=None):
        """游戏窗口模式：捕获前台窗口（或指定窗口）客户区，仅翻译游戏内容。
        进入游戏窗口时默认关闭自动翻译（F6/按钮手动触发，F9 恢复自动）。"""
        region = self._get_foreground_rect(hwnd)
        if not region:
            if hwnd is None:
                self._status("请先点击游戏窗口，再按 F7（或用「选择窗口」按钮选取）")
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
        if self.overlay_mode == "panel":
            # 独立面板不适合覆盖场景，切到覆盖模式；歌词模式与游戏窗口捕获兼容，保持
            self._do_toggle_overlay_mode(target="inplace")
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

    # ---------- 悬浮窗模式切换（F11）：inplace 覆盖原文 ↔ panel 独立面板 ----------
    # 桌面歌词现在是输出面板的「歌词」tab（窗口形态切换），不再属于 F11 模式

    _MODE_CYCLE = {"inplace": "panel", "panel": "inplace"}
    _MODE_LABEL = {"inplace": "覆盖原文", "panel": "独立面板"}

    def toggle_overlay_mode(self):
        self.root.after(0, self._do_toggle_overlay_mode)

    def _do_toggle_overlay_mode(self, target=None):
        self.overlay_mode = target or self._MODE_CYCLE[self.overlay_mode]
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
        self._set_stage(f"显示: {self._MODE_LABEL[self.overlay_mode]}", "success")
        self._push_state()

    # ---------- 输出面板窗口（第二窗口，固定尺寸，显示/隐藏切换） ----------

    def _toggle_output_window(self, force_hide=False):
        """显示/隐藏输出面板窗口。窗口常驻（隐藏保活），Vue 状态与历史记录不丢失。"""
        win = self.wm.get_win("panel")
        if not win:
            return
        try:
            if force_hide or getattr(self, "_output_open", False):
                win.hide()
                self._output_open = False
            else:
                self.wm.position_panel()
                win.show()
                self._output_open = True
                # 窗口显示需要一点时间，稍后应用半透明
                threading.Timer(0.3, lambda: self.wm.set_alpha(TITLE_PANEL, 0.4)).start()
            logging.info("输出面板%s", "显示" if self._output_open else "隐藏")
            self._bridge.push("output_state", {"open": self._output_open}, target="main")
        except Exception:
            logging.error("输出面板切换失败:\n%s", traceback.format_exc())

    def _set_panel_shape(self, shape):
        """输出面板窗口形态切换（「歌词」tab 驱动），委托 WindowManager"""
        try:
            self.wm.set_panel_shape(shape)
        except Exception:
            logging.error("面板形态切换失败:\n%s", traceback.format_exc())

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

    # 主控制窗（极简状态卡：固定 300x360，无边框；显示时定位右上角+半透明）
    win_main = webview.create_window(
        "游戏实时翻译", os.path.join(ui_dir, "index.html"), js_api=api,
        width=300, height=360, frameless=True, on_top=True,
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
        app.wm.main_hwnd = find_hwnd(TITLE_MAIN)
        app.wm.position_main()
        if app.wm.main_hwnd:
            app.wm.set_alpha(TITLE_MAIN, 0.4, hwnd=app.wm.main_hwnd)
        logging.info("主控制窗句柄: %s", app.wm.main_hwnd or "未找到！")

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
