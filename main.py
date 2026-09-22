"""
游戏实时翻译工具
链路：游戏窗口/区域监控（帧差检测+变化区域裁剪）→ PaddleOCR（面板归组）
    → 翻译后端（Ollama/LLM云API/DeepL 可切换，逐行并发）→ 置顶覆盖层/面板显示
    F5 截图直译（视觉模型，右侧面板显示截图与完整输出）

热键：F5 截图直译 | F6 立即翻译 | F7 捕获游戏窗口 | F8 框选区域 | F9 暂停/恢复 | F10 切换后端 | F11 覆盖/面板
"""
import sys
import os
import logging
import threading
import queue
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
import tkinter.ttk as ttk
from datetime import datetime

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


# ---------- UI 主题（深色） ----------
UI_BG = "#1e1e2e"        # 窗口底色
UI_PANEL = "#181825"     # 卡片/记录底色
UI_INPUT = "#313244"     # 输入框/按钮底色
UI_INPUT_ACTIVE = "#45475a"
UI_TEXT = "#cdd6f4"      # 主文字
UI_TEXT_DIM = "#6c7086"  # 次要文字
UI_ACCENT = "#89b4fa"    # 强调蓝（状态）
UI_GREEN = "#a6e3a1"
UI_PURPLE = "#cba6f7"
UI_FONT = "Microsoft YaHei UI"


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
        # 自动翻译开关（F7 进入游戏窗口时自动关闭，F9 切换，状态跨重启保存）
        self.paused = not self.config.get("auto_translate", default=True)
        self._last_positioned_key = None  # 覆盖模式去重（与上一帧相同的文本集合不重复翻译）
        self._region_offset = (0, 0)      # 监控区域屏幕偏移（start_monitor 时更新）
        self._game_hwnd = None            # F7 捕获的游戏窗口句柄（Win 键智能屏蔽用）
        self._positioned_history = []     # 覆盖层译文累积（跨帧保留未变化区域的旧译文）

        # 队列：监控线程 -> OCR 线程 -> 翻译线程 -> UI 主线程（流水线并发）
        self.ocr_queue = queue.Queue()
        self.translate_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self._ocr_lock = threading.Lock()

        # ---------- UI ----------
        self.root = tk.Tk()
        self.root.title("游戏实时翻译")
        self.root.geometry("1120x500")
        self.root.configure(bg=UI_BG)
        self.root.attributes("-topmost", True)  # 控制面板永久置顶，方便实时操作
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
        # 首次启动未配置密钥：弹出设置窗口（分发给朋友时各自填写自己的密钥）
        self.root.after(400, self._check_api_config)

    # ---------- API 设置（密钥存 Windows 凭据管理器） ----------

    def _check_api_config(self):
        if not self.config.get_api_key("llm"):
            self.show_settings_dialog()

    def show_settings_dialog(self):
        import webbrowser

        win = tk.Toplevel(self.root)
        win.title("API 设置")
        win.configure(bg=UI_BG, padx=18, pady=14)
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)

        tk.Label(
            win, text="配置 DeepSeek API", font=(UI_FONT, 13, "bold"),
            fg=UI_ACCENT, bg=UI_BG,
        ).pack(anchor="w", pady=(0, 4))
        tk.Label(
            win, text="密钥仅保存在本机 Windows 凭据管理器，不写入任何文件。",
            fg=UI_TEXT_DIM, bg=UI_BG, font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(0, 8))
        tk.Button(
            win, text="① 打开 DeepSeek 平台注册并创建密钥 →",
            command=lambda: webbrowser.open("https://platform.deepseek.com/api_keys"),
            bg=UI_INPUT, fg=UI_TEXT, activebackground=UI_INPUT_ACTIVE,
            activeforeground="white", bd=0, pady=4, cursor="hand2", anchor="w",
        ).pack(fill="x", pady=(0, 10))

        def field(label, default="", show=""):
            tk.Label(win, text=label, fg=UI_TEXT, bg=UI_BG, font=(UI_FONT, 10)).pack(anchor="w")
            var = tk.StringVar(value=default)
            tk.Entry(
                win, textvariable=var, show=show, bg=UI_INPUT, fg=UI_TEXT,
                insertbackground="white", bd=0, relief="flat", font=(UI_FONT, 10), width=44,
            ).pack(fill="x", ipady=5, pady=(3, 8))
            return var

        url_var = field("API 地址（OpenAI 兼容）", self.config.get("llm", "base_url", default="https://api.deepseek.com/v1"))
        key_var = field("API 密钥", show="•")

        def save():
            key = key_var.get().strip()
            if not key:
                win.destroy()
                return
            if url_var.get().strip():
                self.config.set(url_var.get().strip(), "llm", "base_url")
            self.config.set_api_key(key, "llm")
            self.config.save()
            self.status_var.set("API 密钥已保存到 Windows 凭据管理器")
            win.destroy()

        bar = tk.Frame(win, bg=UI_BG)
        bar.pack(fill="x", pady=(4, 0))
        tk.Button(bar, text="取消", command=win.destroy, bg=UI_INPUT, fg=UI_TEXT,
                  activebackground=UI_INPUT_ACTIVE, activeforeground="white", bd=0,
                  pady=4, padx=12, cursor="hand2").pack(side="right", padx=(6, 0))
        tk.Button(bar, text="保存", command=save, bg="#2563eb", fg="white",
                  activebackground="#3b82f6", bd=0, pady=4, padx=16, cursor="hand2").pack(side="right")

    def show_help(self):
        import webbrowser

        win = tk.Toplevel(self.root)
        win.title("使用说明")
        win.configure(bg=UI_BG, padx=18, pady=14)
        win.transient(self.root)
        win.grab_set()
        text = (
            "快速上手：\n"
            "  1. 点「API 设置」填入自己的 DeepSeek 密钥（存在本机凭据管理器）\n"
            "  2. 打开游戏，点击一下游戏画面，按 F7 捕获游戏窗口\n"
            "  3. 按 F6 立即翻译（译文盖在原文上）；F5 截图直译（整屏发给视觉模型）\n"
            "  4. 右侧输入框可直接向 AI 提问，勾选「带画面」可结合当前游戏画面\n\n"
            "热键：F5 截图直译 | F6 立即翻译 | F7 游戏窗口 | F8 框选区域\n"
            "      F9 暂停/恢复自动翻译 | F11 覆盖原文/独立面板\n\n"
            "密钥获取：platform.deepseek.com 注册后创建 API Key"
        )
        tk.Label(win, text=text, justify="left", fg=UI_TEXT, bg=UI_BG,
                 font=(UI_FONT, 10)).pack(anchor="w")
        tk.Button(
            win, text="打开 DeepSeek 平台", command=lambda: webbrowser.open("https://platform.deepseek.com/api_keys"),
            bg=UI_INPUT, fg=UI_TEXT, activebackground=UI_INPUT_ACTIVE, activeforeground="white",
            bd=0, pady=4, cursor="hand2",
        ).pack(pady=(10, 0))

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
        main_frame = tk.Frame(self.root, bg=UI_BG)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # ===== 左列：状态 + 操作 =====
        left = tk.Frame(main_frame, bg=UI_BG)
        left.pack(side="left", fill="y", padx=(0, 12))

        # 大字状态（翻译流程实时提示）
        self.big_status_var = tk.StringVar(value="等待选择区域\n按 F7 捕获游戏窗口")
        self.big_status = tk.Label(
            left, textvariable=self.big_status_var, bg=UI_BG,
            font=(UI_FONT, 15, "bold"), fg=UI_ACCENT,
        )
        self.big_status.pack(pady=(10, 4))

        # 详细状态行
        self.status_var = tk.StringVar(value="")
        tk.Label(
            left, textvariable=self.status_var, bg=UI_BG,
            fg=UI_TEXT_DIM, font=(UI_FONT, 9),
        ).pack(pady=2)

        # 按钮区（两行）
        btns = tk.Frame(left, bg=UI_BG)
        btns.pack(pady=12)
        btn_cfg = dict(
            bg=UI_INPUT, fg=UI_TEXT, activebackground=UI_INPUT_ACTIVE,
            activeforeground="white", bd=0, pady=5, cursor="hand2",
        )
        self.vision_btn = tk.Button(
            btns, text="截图直译 F5", command=self.vision_translate_async,
            bg="#7c3aed", fg="white", activebackground="#8b5cf6",
            bd=0, pady=5, cursor="hand2",
        )
        self.vision_btn.grid(row=0, column=0, padx=3, pady=3, sticky="ew")
        self.trigger_btn = tk.Button(
            btns, text="立即翻译 F6", command=self.trigger_now_async,
            bg="#2d6a4f", fg="white", activebackground="#40916c",
            bd=0, pady=5, cursor="hand2",
        )
        self.trigger_btn.grid(row=0, column=1, padx=3, pady=3, sticky="ew")
        tk.Button(
            btns, text="游戏窗口 F7", command=self.set_fullscreen_async,
            bg="#2563eb", fg="white", activebackground="#3b82f6",
            bd=0, pady=5, cursor="hand2",
        ).grid(row=0, column=2, padx=3, pady=3, sticky="ew")

        self.pause_btn = tk.Button(
            btns, text="恢复 (F9)" if self.paused else "暂停 (F9)",
            command=self.toggle_pause, **btn_cfg,
        )
        self.pause_btn.grid(row=1, column=0, padx=3, pady=3, sticky="ew")
        self.mode_btn = tk.Button(btns, text="", command=self.toggle_overlay_mode, **btn_cfg)
        self.mode_btn.grid(row=1, column=1, padx=3, pady=3, sticky="ew")
        tk.Button(btns, text="框选区域 F8", command=self.select_region_async, **btn_cfg).grid(
            row=1, column=2, padx=3, pady=3, sticky="ew",
        )
        self._refresh_mode_btn()

        # API 设置（密钥存 Windows 凭据管理器，分发给他人时各自填写）
        tk.Button(
            btns, text="API 设置", command=self.show_settings_dialog, **btn_cfg,
        ).grid(row=2, column=0, padx=3, pady=3, sticky="ew")
        tk.Button(
            btns, text="使用说明", command=self.show_help, **btn_cfg,
        ).grid(row=2, column=1, padx=3, pady=3, sticky="ew")

        # ===== 右列：问 AI 输入条 + 输出历史 =====
        right = tk.Frame(main_frame, bg=UI_BG)
        right.pack(side="right", fill="both", expand=True)

        # 问 AI 输入条
        ask_bar = tk.Frame(right, bg=UI_BG)
        ask_bar.pack(fill="x", pady=(0, 6))
        self.ask_entry_var = tk.StringVar()
        self.ask_entry = tk.Entry(
            ask_bar, textvariable=self.ask_entry_var,
            bg=UI_INPUT, fg=UI_TEXT, insertbackground="white",
            bd=0, relief="flat", font=(UI_FONT, 11),
        )
        self.ask_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))
        self.ask_entry.bind("<Return>", lambda e: self.ask_ai_async())
        self.with_image_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            ask_bar, text="带画面", variable=self.with_image_var,
            bg=UI_BG, fg=UI_TEXT_DIM, selectcolor=UI_INPUT,
            activebackground=UI_BG, activeforeground=UI_TEXT, bd=0,
            font=(UI_FONT, 9),
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            ask_bar, text="提问", command=self.ask_ai_async,
            bg="#2563eb", fg="white", activebackground="#3b82f6",
            bd=0, pady=4, padx=14, cursor="hand2", font=(UI_FONT, 10, "bold"),
        ).pack(side="right")

        # 输出历史（截图直译 / 问 AI 混合，每条 = 时间 + 缩略图 + 输出）
        tk.Label(
            right, text="输出历史（F5 截图直译 · 提问回答）",
            font=(UI_FONT, 9, "bold"), fg=UI_PURPLE, anchor="w", bg=UI_BG,
        ).pack(fill="x", pady=(0, 2))

        self.vision_canvas = tk.Canvas(right, highlightthickness=0, bg=UI_BG)
        vsb = ttk.Scrollbar(right, orient="vertical", command=self.vision_canvas.yview)
        self.vision_canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.vision_canvas.pack(side="left", fill="both", expand=True)
        self.vision_inner = tk.Frame(self.vision_canvas, bg=UI_BG)
        self._vision_win = self.vision_canvas.create_window((0, 0), window=self.vision_inner, anchor="nw")
        self.vision_inner.bind(
            "<Configure>",
            lambda e: self.vision_canvas.configure(scrollregion=self.vision_canvas.bbox("all")),
        )
        self.vision_canvas.bind(
            "<Configure>",
            lambda e: self.vision_canvas.itemconfigure(self._vision_win, width=e.width),
        )
        self._vision_first = None  # 最早一条记录（新记录插到它上面，保持最新在顶部）
        self._bind_wheel(self.vision_canvas)
        self._bind_wheel(self.vision_inner)

    def _bind_wheel(self, widget):
        """鼠标滚轮：进入面板区域时接管滚动，离开后释放"""
        widget.bind("<Enter>", lambda e: widget.bind_all(
            "<MouseWheel>", lambda ev: self.vision_canvas.yview_scroll(int(-ev.delta / 120), "units")))
        widget.bind("<Leave>", lambda e: widget.unbind_all("<MouseWheel>"))

    def _vision_record_start(self, thumb, title="截图直译", prefix=""):
        """新增一条输出记录骨架（时间戳 + 标题 + 可选缩略图 + 空输出区），
        正文由后续 vision_delta 流式追加（打字机效果）"""
        from PIL import ImageTk

        rec = tk.Frame(self.vision_inner, bg=UI_PANEL, bd=0, highlightthickness=1,
                       highlightbackground=UI_INPUT)
        if self._vision_first is None:
            rec.pack(fill="x", pady=5, padx=2)
            self._vision_first = rec
        else:
            rec.pack(fill="x", pady=5, padx=2, before=self._vision_first)

        tk.Label(
            rec, text=f"🕐 {datetime.now().strftime('%H:%M:%S')}  {title}",
            font=(UI_FONT, 9, "bold"), fg=UI_PURPLE, anchor="w", bg=UI_PANEL,
        ).pack(fill="x", padx=8, pady=(6, 3))

        if thumb is not None:
            photo = ImageTk.PhotoImage(thumb)
            lbl = tk.Label(rec, image=photo, bd=0, bg=UI_PANEL)
            lbl.image = photo  # 持有引用防 GC 回收
            lbl.pack(padx=8, pady=3)
            self._bind_wheel(lbl)

        body = tk.Text(
            rec, font=(UI_FONT, 10), wrap="word", bd=0,
            bg=UI_PANEL, fg=UI_TEXT, padx=8, pady=6, height=6,
            insertbackground="white",
        )
        if prefix:
            body.insert("end", prefix)
        body.pack(fill="x", padx=6, pady=(0, 6))
        self._bind_wheel(body)
        self._vision_body = body  # vision_delta 持续往这里追加

        self.vision_canvas.update_idletasks()
        self.vision_canvas.yview_moveto(0)  # 最新记录滚动到顶部

    # ---------- 状态提示 ----------

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
        return rects

    def _set_stage(self, text, color="#409eff", revert_to=None, revert_ms=2000):
        """更新大字状态；revert_to 给定时，revert_ms 后自动回落"""
        self.big_status_var.set(text)
        self.big_status.configure(fg=color)
        revert_id = getattr(self, "_stage_revert_id", None)
        if revert_id:
            try:
                self.root.after_cancel(revert_id)
            except Exception:
                pass
            self._stage_revert_id = None
        if revert_to:
            def _revert():
                self.big_status_var.set(revert_to)
                self.big_status.configure(fg="#409eff")
            self._stage_revert_id = self.root.after(revert_ms, _revert)

    def _monitor_status_text(self):
        if self.paused:
            return "⏸ 已暂停"
        if not self.config.region:
            return "等待选择区域\n按 F7 捕获游戏窗口"
        return "● 监控中"

    def _refresh_mode_btn(self):
        label = "覆盖原文" if self.overlay_mode == "inplace" else "独立面板"
        self.mode_btn.config(text=f"显示: {label} (F11)")

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
        # 记录区域在屏幕上的偏移，覆盖模式绘制时把 OCR 相对坐标转换为屏幕绝对坐标
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
            self.ui_queue.put(("stage", "⟳ 正在识别…"))
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
                    # 兜底过滤：剔除误截到的本工具自身 UI 文案（按钮/状态文字）
                    blocks = [b for b in blocks if not _is_own_ui_text(b.get("text", ""))]
                    ox, oy = origin
                    for b in blocks:
                        bx = b["box"]
                        b["box"] = [bx[0] + ox, bx[1] + oy, bx[2] + ox, bx[3] + oy]
                    logging.info("OCR 完成：%d 行合并为 %d 个文本块", len(items), len(blocks))
                    if blocks:
                        self.ui_queue.put(("stage", "⟳ 正在翻译…"))
                        self.translate_queue.put(("positioned", blocks))
                else:
                    # 面板模式：整段识别，交给翻译线程
                    text = self.ocr_engine.extract(frame)
                    if text and not _is_own_ui_text(text):
                        self.ui_queue.put(("stage", "⟳ 正在翻译…"))
                        self.translate_queue.put(("panel", text))
            except Exception as e:
                logging.error("OCR 处理失败:\n%s", traceback.format_exc())
                self.ui_queue.put(("status", f"处理失败: {e}"))

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
                        # （对话更新时只刷新对话区，侧边栏/按钮的旧译文不消失）
                        kept = [
                            old for old in self._positioned_history
                            if not any(self._boxes_overlap(old["box"], n["box"]) for n in positioned)
                        ]
                        self._positioned_history = kept + positioned
                        if len(self._positioned_history) > 30:
                            self._positioned_history = self._positioned_history[-30:]
                        logging.info("翻译完成：%d 块（累计 %d 块）", len(positioned), len(self._positioned_history))
                        self.ui_queue.put(("stage_done", len(positioned)))
                        self.ui_queue.put(("positioned", list(self._positioned_history)))
                else:
                    translated = self.translator.translate(payload)
                    if translated is not None:
                        self.ui_queue.put(("stage_done", None))
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
                elif kind == "stage":
                    # 翻译流程阶段提示（正在识别/正在翻译）
                    self._set_stage(item[1], "#e6a23c")
                elif kind == "stage_done":
                    count = item[1]
                    text = f"✓ 翻译完成（{count} 块）" if count else "✓ 翻译完成"
                    self._set_stage(text, "#67c23a", revert_to=self._monitor_status_text())
                elif kind == "translation":
                    _, original, translated = item
                    self.overlay.update_translation(translated, self.translator.backend, self.paused)
                elif kind == "positioned":
                    _, positioned = item
                    self.overlay.update_positioned(positioned)
                elif kind == "vision_start":
                    self._vision_record_start(item[1], title="截图直译")
                elif kind == "ask_start":
                    _, question, thumb = item
                    self._vision_record_start(thumb, title="问 AI", prefix=f"❓ {question}\n\n")
                    self._vision_body.see("end")
                elif kind == "vision_delta":
                    body = getattr(self, "_vision_body", None)
                    if body is not None:
                        body.configure(state="normal")
                        body.insert("end", item[1])
                        body.see("end")
                        body.configure(state="disabled")
                elif kind == "vision_done":
                    body = getattr(self, "_vision_body", None)
                    if body is not None:
                        # 按最终内容行数调整高度（流式期间固定高度+自动滚动）
                        n_lines = max(4, min(30, body.get("1.0", "end").count("\n") + 1))
                        body.configure(height=n_lines)
                        self._vision_body = None
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    # ---------- 热键动作（pynput 回调线程 → 调度到主线程） ----------

    def select_region_async(self):
        self.root.after(0, self.do_select_region)

    def set_fullscreen_async(self):
        self.root.after(0, self.do_capture_foreground)

    def trigger_now_async(self):
        # 手动翻译不依赖 UI 线程，直接在热键线程执行
        if self.monitor:
            self.monitor.capture_now()

    def vision_translate_async(self):
        """截图直译（F5）：整屏截图直接发给视觉模型，译文显示在右侧面板"""
        if self.monitor:
            threading.Thread(target=self._vision_worker, daemon=True).start()

    def ask_ai_async(self):
        """问 AI：输入框的问题发给 LLM（可勾选附带当前游戏画面给视觉模型）"""
        question = (self.ask_entry_var.get() or "").strip()
        if not question:
            self.ask_entry.focus_set()
            return
        self.ask_entry_var.set("")
        threading.Thread(target=self._ask_worker, args=(question,), daemon=True).start()

    def _ask_worker(self, question):
        try:
            from PIL import Image
            with_image = self.with_image_var.get()

            thumb = None
            if with_image and self.monitor:
                self.ui_queue.put(("stage", "⟳ 截图发送中…"))
                frame = self.monitor.clean_grab()
                if frame is not None:
                    thumb = Image.fromarray(frame)
                    tw = 340
                    if thumb.width > tw:
                        thumb = thumb.resize((tw, max(1, round(thumb.height * tw / thumb.width))))
            else:
                frame = None

            self.ui_queue.put(("stage", "⟳ AI 回答中…"))
            self.ui_queue.put(("ask_start", question, thumb))

            got_reasoning = got_content = False
            for kind, chunk in vision.ask_stream(question, frame, self.config, with_image):
                if kind == "reasoning":
                    if not got_reasoning:
                        got_reasoning = True
                        self.ui_queue.put(("vision_delta", "──── 思考过程 ────\n"))
                    self.ui_queue.put(("vision_delta", chunk))
                else:
                    if not got_content:
                        got_content = True
                        if got_reasoning:
                            self.ui_queue.put(("vision_delta", "\n\n──── 回答 ────\n"))
                    self.ui_queue.put(("vision_delta", chunk))

            self.ui_queue.put(("vision_done", None))
            self.ui_queue.put(("stage_done", None))
            logging.info("问 AI 完成（%s）", "带画面" if with_image else "纯文本")
        except Exception as e:
            logging.error("问 AI 失败:\n%s", traceback.format_exc())
            self.ui_queue.put(("vision_delta", f"\n[问 AI 失败: {e}]"))
            self.ui_queue.put(("vision_done", None))

    def _vision_worker(self):
        try:
            from PIL import Image

            self.ui_queue.put(("stage", "⟳ 截图发送中…"))
            # 优先游戏窗口直接抓取（悬浮窗永不混入），否则屏幕+隐藏自身窗口
            frame = self.monitor.clean_grab()
            if frame is None:
                self.ui_queue.put(("vision_delta", "[截图失败]"))
                self.ui_queue.put(("vision_done", None))
                return

            # 截图缩略图（在记录中体现本次翻译的是哪张图）
            thumb = Image.fromarray(frame)
            tw = 340
            if thumb.width > tw:
                thumb = thumb.resize((tw, max(1, round(thumb.height * tw / thumb.width))))

            self.ui_queue.put(("stage", "⟳ 视觉模型翻译中…"))
            self.ui_queue.put(("vision_start", thumb))
            logging.info("截图直译：发送 %dx%d 给视觉模型（流式）", frame.shape[1], frame.shape[0])

            got_reasoning = got_content = False
            for kind, chunk in vision.translate_screenshot_stream(frame, self.config):
                if kind == "reasoning":
                    if not got_reasoning:
                        got_reasoning = True
                        self.ui_queue.put(("vision_delta", "──── 思考过程 ────\n"))
                    self.ui_queue.put(("vision_delta", chunk))
                else:
                    if not got_content:
                        got_content = True
                        if got_reasoning:
                            self.ui_queue.put(("vision_delta", "\n\n──── 译文 ────\n"))
                    self.ui_queue.put(("vision_delta", chunk))

            self.ui_queue.put(("vision_done", None))
            self.ui_queue.put(("stage_done", None))
            logging.info("截图直译完成（流式）")
        except Exception as e:
            logging.error("截图直译失败:\n%s", traceback.format_exc())
            self.ui_queue.put(("vision_delta", f"\n[截图直译失败: {e}]"))
            self.ui_queue.put(("vision_done", None))

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
        """游戏窗口模式：捕获前台窗口客户区（自动排除桌面/任务栏），仅翻译游戏内容。
        进入游戏窗口时默认关闭自动翻译（F6/按钮手动触发，F9 恢复自动）。"""
        region = self._get_foreground_rect()
        if not region:
            self.status_var.set("请先点击游戏窗口，再按 F7（工具自身窗口会被排除）")
            return
        # 本工具进程是 DPI Aware 的，GetClientRect/ClientToScreen 返回物理像素
        self.config.region = region
        # 进入游戏窗口：默认关闭自动翻译，仅手动触发（F6/按钮），F9 可恢复
        self.config.set(False, "auto_translate")
        self.config.save()
        self.paused = True
        self.pause_btn.config(text="恢复 (F9)")
        self.status_var.set(f"监控中 · 游戏窗口 {region}（自动翻译已关闭）")
        self._positioned_history = []  # 换了监控区域，清空旧译文
        self._set_stage("⏸ 自动翻译已关闭\nF6/F5 手动 · F9 恢复自动")
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
            self._game_hwnd = None  # 框选区域与游戏窗口不再对应，回退屏幕截图模式
            self.status_var.set(f"监控中 · 区域 {region_phys}")
            self._set_stage("● 监控中")
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
        self.config.set(not self.paused, "auto_translate")  # 状态跨重启保存
        self.config.save()
        self._set_stage(self._monitor_status_text())
        self.overlay.update_status(self.translator.backend, self.paused)

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
        self._positioned_history = []

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
            secrets.set_api_key("selftest-ok")
            ok = secrets.get_api_key() == "selftest-ok"
            secrets.delete_api_key()
            logging.info("凭据管理器: %s", "可用" if ok else "不可用")
        except Exception:
            logging.error("凭据管理器失败:\n%s", traceback.format_exc())
        logging.info("=== 自测结束，详见同目录 game-translator.log ===")
    else:
        App().run()
