"""置顶悬浮译文窗口：无边框、半透明、可拖动、显示当前后端与状态"""
import tkinter as tk


class OverlayWindow:
    def __init__(self, root, config):
        self.config = config
        cfg = config.get("overlay", default={})

        self.win = tk.Toplevel(root)
        self.win.title("译文")
        self.win.overrideredirect(True)                  # 无边框
        self.win.attributes("-topmost", True)            # 永远置顶
        self.win.attributes("-alpha", cfg.get("opacity", 0.92))
        self.win.configure(bg="#1e1e2e")

        self.font_size = cfg.get("font_size", 13)
        self.max_width = cfg.get("max_width", 560)

        # 状态栏（后端/暂停状态）
        self.status_var = tk.StringVar(value="就绪")
        self.status = tk.Label(
            self.win, textvariable=self.status_var,
            bg="#1e1e2e", fg="#7f8ea3", anchor="w",
            font=("Microsoft YaHei UI", 9),
        )
        self.status.pack(fill="x", padx=12, pady=(8, 0))

        # 译文内容
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
        self._hide()

    # ---------- 显示控制 ----------

    def _place(self, x, y):
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = self.win.winfo_reqheight()
        # 限制在屏幕内
        x = max(0, min(x, self.win.winfo_screenwidth() - w))
        y = max(0, min(y, self.win.winfo_screenheight() - h))
        self.win.geometry(f"+{x}+{y}")

    def show(self):
        self.win.deiconify()

    def _hide(self):
        self.win.withdraw()

    # ---------- 拖动 ----------

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

    # ---------- 内容更新（仅主线程调用） ----------

    def update_translation(self, text, backend, paused):
        self.text_var.set(text or "（未识别到文本）")
        state = "⏸ 已暂停" if paused else "● 监控中"
        self.show()

    def update_status(self, backend, paused):
        state = "⏸ 已暂停" if paused else "● 监控中"
        self.status_var.set(f"{state}  |  后端: {backend}  |  拖动移动 · F7全屏 F8选区 F9暂停 F10切换后端")
        if paused:
            self.status.configure(fg="#e0af68")
        else:
            self.status.configure(fg="#7f8ea3")
