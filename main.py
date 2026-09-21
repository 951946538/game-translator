"""
游戏实时翻译工具
链路：区域/全屏监控（帧差检测）→ PaddleOCR → 翻译后端（Ollama/LLM云API/DeepL 可切换）→ 置顶悬浮窗

热键：F7 全屏模式 | F8 重新选区 | F9 暂停/恢复 | F10 切换翻译后端
"""
import sys
import threading
import queue

# Windows 控制台中文输出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import tkinter as tk
from tkinter import scrolledtext

from app.config import Config
from app.capture import RegionMonitor
from app.ocr_engine import OCREngine
from app.translator import Translator
from app.overlay import OverlayWindow
from app.region_select import select_region
from app.hotkeys import HotkeyManager


class App:
    def __init__(self):
        self.config = Config()
        self.translator = Translator(self.config)

        self.ocr_engine = None          # PaddleOCR 首次使用时才初始化（启动快）
        self.monitor = None
        self.paused = False

        # 队列：监控线程 -> OCR 工作线程 -> UI 主线程
        self.ocr_queue = queue.Queue()
        self.ui_queue = queue.Queue()

        # ---------- UI ----------
        self.root = tk.Tk()
        self.root.title("游戏实时翻译")
        self.root.geometry("420x420")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_panel()
        self.overlay = OverlayWindow(self.root, self.config)

        # OCR 工作线程
        threading.Thread(target=self._ocr_worker, daemon=True).start()
        # UI 队列轮询
        self.root.after(100, self._poll_ui_queue)

    # ---------- 控制面板 ----------

    def _build_panel(self):
        tk.Label(
            self.root, text="游戏实时翻译工具", font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(pady=(12, 4))

        self.status_var = tk.StringVar(value="未选择监控区域，按 F8 或点击下方按钮框选")
        tk.Label(self.root, textvariable=self.status_var, fg="#555", wraplength=380).pack(pady=2)

        btns = tk.Frame(self.root)
        btns.pack(pady=8)
        tk.Button(btns, text="全屏模式 (F7)", command=self.set_fullscreen_async).grid(row=0, column=0, padx=4)
        tk.Button(btns, text="框选区域 (F8)", command=lambda: self.select_region_async()).grid(row=0, column=1, padx=4)
        self.pause_btn = tk.Button(btns, text="暂停 (F9)", command=self.toggle_pause)
        self.pause_btn.grid(row=0, column=2, padx=4)
        self.backend_btn = tk.Button(btns, text="", command=self.switch_backend)
        self.backend_btn.grid(row=0, column=3, padx=4)
        self._refresh_backend_btn()

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
        )
        self.monitor.start()

    def stop_monitor(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor = None

    def _on_stable_frame(self, frame):
        """监控线程回调：画面稳定，交给 OCR 队列"""
        if not self.paused:
            self.ocr_queue.put(frame)

    def _ocr_worker(self):
        """OCR + 翻译工作线程"""
        while True:
            frame = self.ocr_queue.get()
            if frame is None:
                break
            try:
                if self.ocr_engine is None:
                    self.ui_queue.put(("status", "正在加载 PaddleOCR 模型（首次约 10-30 秒）…"))
                    self.ocr_engine = OCREngine(lang="en")
                    self.ui_queue.put(("status", f"监控中 · {self._region_text(self.config.region)}"))

                text = self.ocr_engine.extract(frame)
                if not text:
                    continue

                translated = self.translator.translate(text)
                if translated is not None:
                    self.ui_queue.put(("translation", text, translated))
            except Exception as e:
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
        self.root.after(0, self.do_set_fullscreen)

    def do_set_fullscreen(self):
        """全屏模式：监控整个主屏，无需框选（全屏游戏建议用无边框窗口模式）"""
        self.config.region = "fullscreen"
        self.status_var.set("监控中 · 全屏模式")
        self.start_monitor()
        self.overlay.update_status(self.translator.backend, self.paused)

    @staticmethod
    def _region_text(region):
        return "全屏" if region == "fullscreen" else str(region)

    def do_select_region(self):
        region = select_region(self.root)
        if region:
            self.config.region = region
            self.status_var.set(f"监控中 · 区域 {region}")
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

    # ---------- 生命周期 ----------

    def on_close(self):
        self.stop_monitor()
        self.ocr_queue.put(None)
        self.config.save()
        self.root.destroy()

    def run(self):
        # 已有保存区域（含全屏模式）则直接开始监控
        if self.config.region:
            self.status_var.set(f"监控中 · {self._region_text(self.config.region)}")
            self.start_monitor()
        self.overlay.update_status(self.translator.backend, self.paused)

        hotkeys = HotkeyManager(
            on_fullscreen=self.set_fullscreen_async,
            on_select_region=self.select_region_async,
            on_toggle_pause=self.toggle_pause,
            on_switch_backend=self.switch_backend,
        )
        hotkeys.start()

        self.root.mainloop()


if __name__ == "__main__":
    App().run()
