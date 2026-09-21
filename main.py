"""
游戏实时翻译工具
链路：区域/全屏监控（帧差检测）→ PaddleOCR → 翻译后端（Ollama/LLM云API/DeepL 可切换）→ 置顶悬浮窗

热键：F7 全屏模式 | F8 重新选区 | F9 暂停/恢复 | F10 切换翻译后端
"""
import sys
import os
import logging
import threading
import queue
import traceback

# 日志写入文件，便于排查问题
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
from tkinter import scrolledtext

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
from app.hotkeys import HotkeyManager, VK_F7, VK_F8, VK_F9, VK_F10, VK_F11, VK_LWIN, VK_RWIN
from app.textblock import group_lines


class App:
    window_ids = []  # 本工具所有窗口的 winfo_id（用于 F7 排除自身窗口）

    def __init__(self):
        App.window_ids = []
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
        self.paused = False
        self._last_positioned_key = None  # 覆盖模式去重（与上一帧相同的文本集合不重复翻译）
        self._region_offset = (0, 0)      # 监控区域屏幕偏移（start_monitor 时更新）
        self._game_hwnd = None            # F7 捕获的游戏窗口句柄（Win 键智能屏蔽用）

        # 队列：监控线程 -> OCR 线程 -> 翻译线程 -> UI 主线程（流水线并发）
        self.ocr_queue = queue.Queue()
        self.translate_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self._ocr_lock = threading.Lock()

        # ---------- UI ----------
        self.root = tk.Tk()
        self.root.title("游戏实时翻译")
        self.root.geometry("440x440")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.overlay_mode = self.config.get("overlay_mode", default="inplace")
        self._build_panel()

        # ---- 屏幕坐标系校准 ----
        # mss/OCR 使用物理像素；tkinter 在 DPI 感知失败时使用逻辑像素。
        # 记录两者比例，框选/绘制时统一换算，彻底解决高分屏（如 3800x2000）偏移问题。
        self.root.update_idletasks()
        import mss as _mss
        _MSS = getattr(_mss, "MSS", _mss.mss)  # 兼容 mss 9.x(mss) / 10.x(MSS)
        with _MSS() as _sct:
            _mon = _sct.monitors[1]
        self._dpi_fx = self.root.winfo_screenwidth() / _mon["width"]
        self._dpi_fy = self.root.winfo_screenheight() / _mon["height"]
        logging.info(
            "屏幕校准: tkinter=%dx%d, mss=%dx%d, 坐标换算系数=(%.3f, %.3f)",
            self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
            _mon["width"], _mon["height"], self._dpi_fx, self._dpi_fy,
        )

        self.overlay = OverlayWindow(self.root, self.config, mode=self.overlay_mode)
        App.window_ids.append(self.root.winfo_id())
        App.window_ids.append(self.overlay.win.winfo_id())

        # OCR 线程 + 翻译线程（并行流水线），启动即后台预加载模型
        threading.Thread(target=self._ocr_worker, daemon=True).start()
        threading.Thread(target=self._translate_worker, daemon=True).start()
        threading.Thread(target=self._preload_ocr, daemon=True).start()
        # UI 队列轮询
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

    # ---------- 控制面板 ----------

    def _build_panel(self):
        tk.Label(
            self.root, text="游戏实时翻译工具", font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(pady=(12, 4))

        self.status_var = tk.StringVar(value="未选择监控区域，按 F8 或点击下方按钮框选")
        tk.Label(self.root, textvariable=self.status_var, fg="#555", wraplength=380).pack(pady=2)

        btns = tk.Frame(self.root)
        btns.pack(pady=8)
        tk.Button(btns, text="游戏窗口 (F7)", command=self.set_fullscreen_async).grid(row=0, column=0, padx=3)
        tk.Button(btns, text="框选区域 (F8)", command=lambda: self.select_region_async()).grid(row=0, column=1, padx=3)
        self.pause_btn = tk.Button(btns, text="暂停 (F9)", command=self.toggle_pause)
        self.pause_btn.grid(row=0, column=2, padx=3)
        self.backend_btn = tk.Button(btns, text="", command=self.switch_backend)
        self.backend_btn.grid(row=0, column=3, padx=3)
        self.mode_btn = tk.Button(btns, text="", command=self.toggle_overlay_mode)
        self.mode_btn.grid(row=0, column=4, padx=3)
        self._refresh_backend_btn()
        self._refresh_mode_btn()

    def _refresh_mode_btn(self):
        label = "覆盖原文" if self.overlay_mode == "inplace" else "独立面板"
        self.mode_btn.config(text=f"显示: {label} (F11)")

        tk.Label(self.root, text="翻译历史", font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(8, 0))
        self.history = scrolledtext.ScrolledText(
            self.root, font=("Microsoft YaHei UI", 10), wrap="word", state="disabled",
        )
        self.history.pack(fill="both", expand=True, padx=10, pady=(4, 10))

    def _refresh_backend_btn(self):
        self.backend_btn.config(text=f"后端: {self.translator.backend} (F10)")

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
            trigger_mode=self.config.get("trigger_mode", default="input"),
            input_idle_ms=self.config.get("input_idle_ms", default=3000),
        )
        # 记录区域在屏幕上的偏移，覆盖模式绘制时把 OCR 相对坐标转换为屏幕绝对坐标
        self._region_offset = self.monitor.offset
        self.monitor.start()

    def stop_monitor(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor = None

    def _on_stable_frame(self, frame, origin):
        """监控线程回调：画面稳定，交给 OCR 队列（frame 为变化区域裁剪，origin 为其屏幕坐标）"""
        if not self.paused:
            self.ocr_queue.put((frame, origin))

    def _ocr_worker(self):
        """OCR 线程：丢弃积压旧帧只处理最新，识别结果交给翻译线程（流水线并发）"""
        while True:
            item = self.ocr_queue.get()
            if item is None:
                break
            # 处理耗时可能超过截屏间隔：丢弃积压旧帧，只处理最新画面
            while True:
                try:
                    item = self.ocr_queue.get_nowait()
                except queue.Empty:
                    break
            frame, origin = item
            try:
                with self._ocr_lock:
                    if self.ocr_engine is None:
                        self.ui_queue.put(("status", "正在加载 PaddleOCR 模型（首次约 10-30 秒）…"))
                        self.ocr_engine = OCREngine(
                            lang="en",
                            min_score=self.config.get("ocr_min_score", default=0.6),
                            high_accuracy=self.config.get("ocr_high_accuracy", default=False),
                        )
                        self.ui_queue.put(("status", f"监控中 · {self._region_text(self.config.region)}"))

                if self.overlay_mode == "inplace":
                    # 覆盖模式：带坐标识别 + 按文本框面板归组（保证换行长句的翻译上下文完整）
                    items = self.ocr_engine.extract_detail(frame)
                    blocks = group_lines(items, frame=frame)
                    ox, oy = origin
                    for b in blocks:
                        bx = b["box"]
                        b["box"] = [bx[0] + ox, bx[1] + oy, bx[2] + ox, bx[3] + oy]
                    logging.info("OCR 完成：%d 行合并为 %d 个文本块", len(items), len(blocks))
                    if blocks:
                        self.translate_queue.put(("positioned", blocks))
                else:
                    # 面板模式：整段识别，交给翻译线程
                    text = self.ocr_engine.extract(frame)
                    if text:
                        self.translate_queue.put(("panel", text))
            except Exception as e:
                logging.error("OCR 处理失败:\n%s", traceback.format_exc())
                self.ui_queue.put(("status", f"处理失败: {e}"))

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
                        logging.info("翻译完成：%d 行", len(positioned))
                        self.ui_queue.put(("positioned", positioned))
                else:
                    translated = self.translator.translate(payload)
                    if translated is not None:
                        self.ui_queue.put(("translation", payload, translated))
            except Exception as e:
                logging.error("翻译处理失败:\n%s", traceback.format_exc())
                self.ui_queue.put(("status", f"处理失败: {e}"))

    # ---------- UI 队列消费 ----------

    def _poll_ui_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status_var.set(item[1])
                elif kind == "translation":
                    _, original, translated = item
                    self.overlay.update_translation(translated, self.translator.backend, self.paused)
                    self._append_history(original, translated)
                elif kind == "positioned":
                    _, positioned = item
                    self.overlay.update_positioned(positioned)
                    self._append_history(
                        "(覆盖模式译文)", "\n".join(p["text"] for p in positioned)
                    )
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    def _append_history(self, original, translated):
        self.history.configure(state="normal")
        self.history.insert("1.0", f"—— {self._now()} ——\n[原文]\n{original}\n\n[译文]\n{translated}\n\n")
        self.history.see("1.0")
        self.history.configure(state="disabled")

    @staticmethod
    def _now():
        import datetime
        return datetime.datetime.now().strftime("%H:%M:%S")

    # ---------- 热键动作（pynput 回调线程 → 调度到主线程） ----------

    def select_region_async(self):
        self.root.after(0, self.do_select_region)

    def set_fullscreen_async(self):
        self.root.after(0, self.do_capture_foreground)

    def _get_foreground_rect(self):
        """获取前台窗口客户区的屏幕物理坐标 (x, y, w, h)，并记录游戏窗口句柄。
        无法获取或目标是自己时返回 None"""
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        # 排除本工具自己的窗口（主面板/悬浮窗）
        GA_ROOT = 2
        my_windows = set()
        for wid in App.window_ids:
            try:
                my_windows.add(user32.GetAncestor(user32.GetParent(wid), GA_ROOT))
                my_windows.add(user32.GetAncestor(wid, GA_ROOT))
            except Exception:
                pass
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
        """游戏窗口模式：捕获前台窗口客户区（自动排除桌面/任务栏），仅翻译游戏内容
        前台窗口默认使用覆盖原文显示。使用前先点击一下游戏窗口。"""
        region = self._get_foreground_rect()
        if not region:
            self.status_var.set("请先点击游戏窗口，再按 F7（工具自身窗口会被排除）")
            return
        # 本工具进程是 DPI Aware 的，GetClientRect/ClientToScreen 返回物理像素
        self.config.region = region
        self.status_var.set(f"监控中 · 游戏窗口 {region}")
        self.start_monitor()
        if self.overlay_mode != "inplace":
            self._do_toggle_overlay_mode()
        else:
            self.overlay.update_status(self.translator.backend, self.paused)

    def _on_win_key(self):
        """Win 键智能屏蔽：游戏窗口在前台时吞掉（防游戏误触），
        其他情况把按键转发回系统，开始菜单正常弹出"""
        import ctypes
        user32 = ctypes.windll.user32
        if self._game_hwnd and user32.GetForegroundWindow() == self._game_hwnd:
            return  # 游戏前台：吞掉
        # 非游戏前台：转发 Win 键（原按键已被热键吞掉，这里重新注入）
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
            self.status_var.set(f"监控中 · 区域 {region_phys}")
            self.start_monitor()
            self.overlay.update_status(self.translator.backend, self.paused)

    def toggle_pause(self):
        self.root.after(0, self._do_toggle_pause)

    def _do_toggle_pause(self):
        self.paused = not self.paused
        if self.monitor:
            if self.paused:
                self.monitor.pause()
            else:
                self.monitor.resume()
        self.pause_btn.config(text="恢复 (F9)" if self.paused else "暂停 (F9)")
        self.overlay.update_status(self.translator.backend, self.paused)

    def switch_backend(self):
        self.root.after(0, self._do_switch_backend)

    def _do_switch_backend(self):
        new_backend = self.translator.switch_backend()
        self._refresh_backend_btn()
        self.overlay.update_status(new_backend, self.paused)

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
        self._refresh_mode_btn()
        self._last_positioned_key = None  # 切换后强制重新翻译一次

    # ---------- 生命周期 ----------

    def on_close(self):
        self.stop_monitor()
        self.ocr_queue.put(None)
        self.translate_queue.put(None)
        try:
            self._hotkeys.stop()
        except Exception:
            pass
        self.config.save()
        self.root.destroy()

    def run(self):
        # 已有保存区域（含全屏模式）则直接开始监控
        if self.config.region:
            self.status_var.set(f"监控中 · {self._region_text(self.config.region)}")
            self.start_monitor()
        self.overlay.update_status(self.translator.backend, self.paused)

        hotkey_bindings = {
            VK_F7: self.set_fullscreen_async,
            VK_F8: self.select_region_async,
            VK_F9: self.toggle_pause,
            VK_F10: self.switch_backend,
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


if __name__ == "__main__":
    App().run()
