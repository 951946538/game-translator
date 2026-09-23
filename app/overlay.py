"""译文悬浮窗：panel（独立小窗）/ inplace（覆盖原文）/ lyrics（桌面歌词式底部字幕条）"""
import tkinter as tk

# inplace 模式的全透明色（该颜色的像素完全透明）
TRANS_COLOR = "#010101"

# Windows 扩展样式
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020  # 鼠标事件完全穿透
LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002


class LyricsBar:
    """桌面歌词式译文条：无边框置顶半透明深色条，默认贴屏幕底部居中。
    - 可拖动（按住任意处拖）
    - 双击切换锁定（锁定后鼠标完全穿透，不挡游戏）
    - 文本更新时高度自适应并保持底边不动（贴合 galgame 底部文本栏习惯）
    """

    def __init__(self, root, config):
        self.config = config
        cfg = config.get("overlay", default={})
        self.font_size = cfg.get("lyrics_font_size", 13)
        self._alpha = cfg.get("lyrics_opacity", 0.85)
        self._locked = False
        self._last_text = "等待翻译…"

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", self._alpha)
        bg = "#14141f"
        self.win.configure(bg=bg)
        self.label = tk.Label(
            self.win, text=self._last_text, bg=bg, fg="#f0f0f0",
            font=("Microsoft YaHei UI", self.font_size),
            justify="left", anchor="w", wraplength=900, padx=16, pady=12,
        )
        self.label.pack(fill="both", expand=True)

        # 初始位置：底部居中
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w = max(self.win.winfo_reqwidth(), 600)
        self._bottom = sh - 90
        self.win.geometry(f"{w}x{self.win.winfo_reqheight()}+{(sw - w) // 2}+{self._bottom - self.win.winfo_reqheight()}")

        self._enable_drag()
        self._keep_topmost()

    def update_lyrics(self, text):
        """更新译文（多行），高度自适应保持底边"""
        if not text:
            return
        self._last_text = text
        self.label.config(text=text)
        self.win.update_idletasks()
        h = self.win.winfo_reqheight()
        self.win.geometry(f"{self.win.winfo_width()}x{h}+{self.win.winfo_x()}+{self._bottom - h}")

    def _enable_drag(self):
        def on_press(e):
            self._drag = (e.x, e.y, self.win.winfo_rooty() + e.y)

        def on_move(e):
            if hasattr(self, "_drag"):
                x = self.win.winfo_x() + e.x - self._drag[0]
                y = self.win.winfo_y() + e.y - self._drag[1]
                self.win.geometry(f"+{x}+{y}")
                # 拖动后更新底边锚点（高度自适应时以此为基准）
                self._bottom = y + self.win.winfo_height()

        def on_double_click(e):
            self._toggle_lock()

        for w in (self.win, self.label):
            w.bind("<Button-1>", on_press)
            w.bind("<B1-Motion>", on_move)
            w.bind("<Double-Button-1>", on_double_click)

    def _toggle_lock(self):
        """双击：锁定（鼠标穿透，不挡游戏）/ 解锁（可拖动）"""
        self._locked = not self._locked
        self._make_clickthrough(self._locked)
        # 状态提示（2 秒后恢复文本）
        self.label.config(text="🔒 已锁定（双击解锁）" if self._locked else "🔓 已解锁（可拖动，双击锁定）",
                          fg="#a6e3a1" if self._locked else "#e0af68")
        if self._locked:
            # 锁定后鼠标穿透，提示文字留在屏幕上直到下一次译文更新
            self.win.after(1500, lambda: self.label.config(fg="#f0f0f0"))

    def _make_clickthrough(self, on):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if on:
                style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
                style |= WS_EX_LAYERED
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            ctypes.windll.user32.SetLayeredWindowAttributes(
                hwnd, 0, int(self._alpha * 255), LWA_ALPHA,
            )
        except Exception:
            pass

    def _keep_topmost(self):
        """每 2 秒强制刷新置顶"""
        try:
            self.win.attributes("-topmost", False)
            self.win.attributes("-topmost", True)
        except tk.TclError:
            return
        self.win.after(2000, self._keep_topmost)

    def update_status(self, backend, paused):
        """接口兼容（歌词条不显示状态栏）"""
        pass

    def update_positioned(self, items):
        """接口兼容（歌词模式不用坐标绘制，文本更新由 App 分流调用 update_lyrics）"""
        pass


class OverlayWindow:
    def __init__(self, root, config, mode="panel"):
        self.config = config
        self.mode = mode
        cfg = config.get("overlay", default={})

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)                  # 无边框
        self.win.attributes("-topmost", True)            # 永远置顶
        self.font_size = cfg.get("font_size", 13)
        self.max_width = cfg.get("max_width", 560)
        self.pad = cfg.get("pad", 6)  # 覆盖块向外扩大的像素数

        if mode == "inplace":
            # ===== 覆盖模式：全屏透明画布，译文画在原文坐标上，鼠标完全穿透 =====
            self._alpha_value = min(cfg.get("opacity", 0.92), 0.95)
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            self.win.geometry(f"{sw}x{sh}+0+0")
            self.win.attributes("-transparentcolor", TRANS_COLOR)
            self.win.attributes("-alpha", self._alpha_value)
            self.win.configure(bg=TRANS_COLOR)
            self.canvas = tk.Canvas(self.win, bg=TRANS_COLOR, highlightthickness=0)
            self.canvas.pack(fill="both", expand=True)
            self._status_id = self.canvas.create_text(
                12, sh - 28, anchor="w", fill="#8a94a6",
                font=("Microsoft YaHei UI", -10), text="",
            )
            # 鼠标穿透必须在窗口映射之后再设置（过早会被 Tk 重置导致点击被拦截）
            self.win.after(300, self._make_clickthrough)
        else:
            # ===== 面板模式：独立小窗 =====
            self.win.attributes("-alpha", cfg.get("opacity", 0.92))
            self.win.configure(bg="#1e1e2e")

            self.status_var = tk.StringVar(value="就绪")
            self.status = tk.Label(
                self.win, textvariable=self.status_var,
                bg="#1e1e2e", fg="#7f8ea3", anchor="w",
                font=("Microsoft YaHei UI", 9),
            )
            self.status.pack(fill="x", padx=12, pady=(8, 0))

            self.text_var = tk.StringVar(value="等待画面内容…")
            self.body = tk.Label(
                self.win, textvariable=self.text_var,
                bg="#1e1e2e", fg="#e6e6e6", anchor="w", justify="left",
                wraplength=self.max_width - 24,
                font=("Microsoft YaHei UI", self.font_size),
            )
            self.body.pack(fill="both", expand=True, padx=12, pady=(4, 12))

            self._place(60, 60)
            self._enable_drag()
            self.win.withdraw()

        self._keep_topmost()

    # ---------- 置顶保持 ----------

    def _keep_topmost(self):
        """每 2 秒强制刷新置顶：全屏应用/其他置顶窗口可能抢走置顶属性"""
        try:
            self.win.attributes("-topmost", False)
            self.win.attributes("-topmost", True)
            if self.mode == "inplace":
                self._make_clickthrough()  # 一并重申鼠标穿透，防止被系统重置
        except tk.TclError:
            return  # 窗口已销毁
        self.win.after(2000, self._keep_topmost)

    # ---------- 面板模式 ----------

    def _place(self, x, y):
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = self.win.winfo_reqheight()
        x = max(0, min(x, self.win.winfo_screenwidth() - w))
        y = max(0, min(y, self.win.winfo_screenheight() - h))
        self.win.geometry(f"+{x}+{y}")

    def _enable_drag(self):
        def on_press(e):
            self._drag = (e.x, e.y)

        def on_move(e):
            if hasattr(self, "_drag"):
                x = self.win.winfo_x() + e.x - self._drag[0]
                y = self.win.winfo_y() + e.y - self._drag[1]
                self.win.geometry(f"+{x}+{y}")

        for widget in (self.win, self.status, self.body):
            widget.bind("<Button-1>", on_press)
            widget.bind("<B1-Motion>", on_move)

    def update_translation(self, text, backend, paused):
        """面板模式：更新译文文本"""
        if self.mode != "panel":
            return
        self.text_var.set(text or "（未识别到文本）")
        self.win.deiconify()

    # ---------- 覆盖模式 ----------

    def _make_clickthrough(self):
        """给覆盖窗口加鼠标穿透：所有点击直达下层游戏窗口。
        必须直接调用 SetLayeredWindowAttributes 重设透明色+透明度——
        Tk 认为属性已设置不会重调 API，改样式后窗口会停止渲染（译文不显示的根因）。
        """
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not (style & WS_EX_TRANSPARENT):
                style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            # 直接重设：透明色 #010101 (COLORREF=0x010101) + 窗口透明度，双标志
            colorkey = 0x010101
            alpha = int(getattr(self, "_alpha_value", 0.92) * 255)
            ctypes.windll.user32.SetLayeredWindowAttributes(
                hwnd, colorkey, alpha, LWA_COLORKEY | LWA_ALPHA,
            )
        except Exception:
            pass

    def update_positioned(self, items):
        """
        覆盖模式：译文块从上到下直接盖在原文坐标上。
        items: [{"box": [x1,y1,x2,y2], "text": 译文}, ...]
        """
        if self.mode != "inplace":
            return
        self.canvas.delete("all")
        sh = self.win.winfo_screenheight()
        self._status_id = self.canvas.create_text(
            12, sh - 28, anchor="w", fill="#8a94a6",
            font=("Microsoft YaHei UI", -10), text=self._status_text,
        )

        # 从上到下、从左到右绘制
        import math
        for item in sorted(items, key=lambda it: (it["box"][1], it["box"][0])):
            x1, y1, x2, y2 = item["box"]
            text = item["text"]
            if not text:
                continue
            pad = self.pad
            # 水平额外扩展 3%：检测框常略窄于视觉文字（行尾残留问题）
            pad_x = pad + int((x2 - x1) * 0.03)
            # 字号随平均行高自适应且小于原文（多行文本块用块内平均行高）
            line_h = item.get("line_h", y2 - y1) or (y2 - y1)
            font_size = max(9, min(int(line_h * 0.7), 18))

            # ---- 背景尺寸：贴合实际内容，不虚胖 ----
            box_w, box_h = x2 - x1, y2 - y1
            bg_w = box_w + 2 * pad_x
            wrap_w = bg_w  # 文本换行宽度 = 背景宽度，绝不横向溢出
            # 估算行数：中文字符宽≈字号（含少量英文/数字时略窄，估算留 5% 余量）
            chars_per_line = max(4, int(wrap_w / (font_size * 1.05)))
            n_lines = max(1, math.ceil(len(text) / chars_per_line))
            text_h = n_lines * font_size * 1.35

            # 背景高度：盖住原文框即可；
            # 仅按钮/标签类矮块（行高小）加 30% 下缘扩展兜住艺术字下缘
            if line_h < 26:
                base_h = box_h + 2 * pad + int(box_h * 0.3)
            else:
                base_h = box_h + 2 * pad
            bg_h = max(base_h, int(text_h) + 2 * pad)
            by2 = y1 + bg_h  # 顶部对齐原文框，高度不足向下扩展

            # 圆角色块：观感更轻，文字不多时不显得笨重
            self._round_rect(
                x1 - pad_x, y1 - pad, x2 + pad_x, by2,
                r=min(8, bg_h // 4),
                fill="#14141f", outline="",
            )
            self.canvas.create_text(
                (x1 + x2) // 2, (y1 - pad) + bg_h / 2,
                text=text, fill="#f0f0f0", justify="center",
                font=("Microsoft YaHei UI", -font_size),
                width=wrap_w,
            )

    def _round_rect(self, x1, y1, x2, y2, r=6, **kw):
        """圆角矩形（Tk 无原生支持，用 12 点样条多边形模拟）"""
        r = max(2, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
        pts = [
            x1 + r, y1,  x2 - r, y1,
            x2, y1,      x2, y1 + r,
            x2, y2 - r,  x2, y2,
            x2 - r, y2,  x1 + r, y2,
            x1, y2,      x1, y2 - r,
            x1, y1 + r,  x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    # ---------- 状态栏 ----------

    _status_text = ""

    def update_status(self, backend, paused):
        state = "⏸ 已暂停" if paused else "● 监控中"
        self._status_text = (
            f"{state}  |  后端: {backend}  |  F5直译 F6翻译 F7窗口 F8选区 F9暂停 F11显示"
        )
        if self.mode == "panel":
            self.status_var.set(self._status_text)
            self.status.configure(fg="#e0af68" if paused else "#7f8ea3")
        else:
            try:
                self.canvas.itemconfigure(self._status_id, text=self._status_text)
            except tk.TclError:
                pass
