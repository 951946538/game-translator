"""译文悬浮窗：panel（独立小窗）/ inplace（译文覆盖在原文位置上，沉浸式）"""
import tkinter as tk

# inplace 模式的全透明色（该颜色的像素完全透明且鼠标可穿透）
TRANS_COLOR = "#010101"


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
            # ===== 覆盖模式：全屏透明画布，译文画在原文坐标上 =====
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            self.win.geometry(f"{sw}x{sh}+0+0")
            self.win.attributes("-transparentcolor", TRANS_COLOR)
            self.win.attributes("-alpha", min(cfg.get("opacity", 0.92), 0.95))
            self.win.configure(bg=TRANS_COLOR)
            self.canvas = tk.Canvas(self.win, bg=TRANS_COLOR, highlightthickness=0)
            self.canvas.pack(fill="both", expand=True)
            self._status_id = self.canvas.create_text(
                12, sh - 28, anchor="w", fill="#8a94a6",
                font=("Microsoft YaHei UI", 9), text="",
            )
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

    def update_positioned(self, items):
        """
        覆盖模式：译文块画在原文坐标上。
        items: [{"box": [x1,y1,x2,y2], "text": 译文}, ...]
        """
        if self.mode != "inplace":
            return
        self.canvas.delete("all")
        sh = self.win.winfo_screenheight()
        self._status_id = self.canvas.create_text(
            12, sh - 28, anchor="w", fill="#8a94a6",
            font=("Microsoft YaHei UI", 9), text=self._status_text,
        )

        for item in items:
            x1, y1, x2, y2 = item["box"]
            text = item["text"]
            if not text:
                continue
            pad = self.pad
            # 背景块盖住原文（向下多扩 50%：中文译文字数通常少于英文，但按钮艺术字下缘常超出检测框）
            extra_bottom = int((y2 - y1) * 0.5)
            self.canvas.create_rectangle(
                x1 - pad, y1 - pad, x2 + pad, y2 + pad + extra_bottom,
                fill="#14141f", outline="",
            )
            # 字号随原文行高自适应（中文比英文宽，适当放大区域）
            line_h = max(y2 - y1, 14)
            font_size = max(10, min(int(line_h * 0.82), 22))
            self.canvas.create_text(
                (x1 + x2) // 2, (y1 + y2) // 2 + extra_bottom // 2,
                text=text, fill="#f0f0f0", justify="center",
                font=("Microsoft YaHei UI", font_size),
                width=max((x2 - x1) * 2, 120),
            )

    # ---------- 状态栏 ----------

    _status_text = ""

    def update_status(self, backend, paused):
        state = "⏸ 已暂停" if paused else "● 监控中"
        self._status_text = (
            f"{state}  |  后端: {backend}  |  F7全屏 F8选区 F9暂停 F10后端 F11覆盖/面板"
        )
        if self.mode == "panel":
            self.status_var.set(self._status_text)
            self.status.configure(fg="#e0af68" if paused else "#7f8ea3")
        else:
            try:
                self.canvas.itemconfigure(self._status_id, text=self._status_text)
            except tk.TclError:
                pass
