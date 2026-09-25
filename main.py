"""
游戏实时翻译工具

架构（功能互相独立，经 ctx 组装）：
  features/live.py        实时翻译（监控→OCR→翻译流水线，覆盖层/独立面板显示）
  features/screenshot.py  截图直译 + 问 AI（视觉模型，流式输出）
  features/lyrics.py      歌词模式（订阅实时翻译结果，纯展示）
  main.App                组装层：窗口/捕获目标管理，不包含功能逻辑

Win32 全部集中在 app/windows.py（窗口管理）/ app/capture.py（抓取）/
  app/overlay.py（点击穿透），main.py 零 ctypes。
操作全部在输出面板（无全局热键）：选择窗口 / 立即翻译 / 截图直译 / 歌词。
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

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import tkinter as tk

# 开启进程 DPI 感知：tkinter 使用物理像素坐标，与截屏/OCR 一致
# （否则 125%/150% 缩放下覆盖位置整体偏移）
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
from app.translator import Translator
from app.overlay import OverlayWindow
from app.region_select import select_region
from app.windows import (
    WindowManager, find_hwnd, client_rect, window_rect, window_info,
    process_elevated, self_elevated, TITLE_MAIN, TITLE_PANEL,
)
from app.features.live import LiveTranslate
from app.features.screenshot import ScreenshotTranslate
from app.features.lyrics import Lyrics
from webui import EventBridge, PyApi


class App:
    """组装层：管理窗口/热键/捕获目标，功能逻辑全部委托给 features。"""

    window_ids = []  # 本工具所有 tk 窗口的 winfo_id（F7 排除自身用）

    def __init__(self, bridge: EventBridge, wins_provider=None):
        App.window_ids = []
        self._bridge = bridge
        self._status_text = ""
        self._wins_provider = wins_provider or (lambda: {})
        self.wm = WindowManager(self._wins_provider, lambda: App.window_ids)
        self._output_open = False
        self._overlay_hidden = False
        self.config = Config()
        self.translator = Translator(self.config)

        # 进程降为低优先级：游戏优先占用 CPU
        try:
            import psutil
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass

        # ---------- tkinter 侧：隐藏主窗（仅作覆盖层载体与 DPI 校准） ----------
        self.root = tk.Tk()
        self.root.title("游戏实时翻译")
        self.root.withdraw()
        # pythonw 模式下 withdraw 偶发不生效：Win32 级 SW_HIDE 双保险
        try:
            import ctypes
            tk_hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            ctypes.windll.user32.ShowWindow(tk_hwnd, 0)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # lyrics 已是输出面板的 tab（不再是独立显示模式），历史配置归一
        mode = self.config.get("overlay_mode", default="inplace")
        self.overlay_mode = "inplace" if mode == "lyrics" else mode

        # ---- 屏幕坐标系校准（tkinter 逻辑像素 vs 物理像素） ----
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

        self.ui_queue = queue.Queue()  # 仅 tk 线程消费（覆盖层绘制/窗口关闭）
        self._game_hwnd = None         # F7 捕获的游戏窗口句柄
        self._pre_lyrics_region = None  # 歌词模式前的监控区域（离开时恢复）

        # ---------- 组装三个功能（互相独立） ----------
        self.live = LiveTranslate(self)             # 实时翻译
        self.screenshot = ScreenshotTranslate(self)  # 截图直译 + 问 AI
        self.lyrics = Lyrics(bridge)                 # 歌词（订阅实时翻译结果）
        self.wm.lyrics_shape = self.config.get("lyrics_shape", default=None)

        threading.Thread(target=self.wm.keep_on_top_loop, daemon=True).start()
        self.root.after(100, self._poll_ui_queue)

    # ---------- 功能回调（LiveTranslate 的 ctx 接口） ----------

    @property
    def bridge(self):
        """features 的 ctx 约定属性（App 内部叫 _bridge）"""
        return self._bridge

    @property
    def overlay_hidden(self):
        """features 的 ctx 约定属性（App 内部叫 _overlay_hidden）"""
        return self._overlay_hidden

    @property
    def dpi_factors(self):
        return self._dpi_fx, self._dpi_fy

    def on_translation(self, positioned, lines, translated):
        """实时翻译每帧完成的订阅入口：翻译历史 + 歌词。"""
        self._bridge.push("translation", {
            "original": "\n".join(lines),
            "translated": "\n".join(t for t in translated if t),
        })
        self.lyrics.on_translation(positioned)

    def status(self, text):
        self._status_text = text
        self._push_state()

    def refresh_status(self):
        self._status_text = self._monitor_status_text() if self.config.region else ""
        self._push_state()

    def schedule_stage_revert(self):
        """2 秒后状态卡回落到监控状态文本"""
        threading.Timer(2.0, lambda: self._bridge.push(
            "stage", {"text": self._monitor_status_text(), "tone": "info"})).start()

    # ---------- Web UI 状态推送 ----------

    def _push_state(self):
        self._bridge.push("state", {
            "paused": self.live.paused,
            "overlay_mode": self.overlay_mode,
            "overlay_hidden": self._overlay_hidden,
            "region": list(self.config.region) if self.config.region and self.config.region != "fullscreen" else self.config.region,
            "status": self._status_text,
        })

    def _monitor_status_text(self):
        if self.live.paused:
            return "⏸ 已暂停"
        if not self.config.region:
            return "打开输出面板\n点「🪟 选择窗口」捕获游戏"
        return "● 监控中"

    def _set_stage(self, text, tone="info"):
        self._bridge.push("stage", {"text": text, "tone": tone})

    def _toggle_overlay_visible(self):
        """显示/隐藏译文（两种模式都生效：覆盖层清空/重绘；独立面板收起/恢复）"""
        self._overlay_hidden = not self._overlay_hidden
        if self._overlay_hidden:
            self.ui_queue.put(("overlay_hide", None))
        else:
            self.ui_queue.put(("overlay_show", list(self.live.positioned_history)))
        self._push_state()

    # ---------- UI 队列消费（tk 线程：仅覆盖层相关） ----------

    def _poll_ui_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "positioned":
                    _, positioned = item
                    if positioned or not self._overlay_hidden:
                        self.overlay.update_positioned(positioned)
                elif kind == "overlay_clear":
                    self.overlay.clear_translation()
                elif kind == "overlay_hide":
                    self.overlay.set_visible(False)
                elif kind == "overlay_show":
                    self.overlay.set_visible(True, item[1])
                elif kind == "translation":
                    if not self._overlay_hidden:  # 隐藏期间新译文不唤醒面板
                        self.overlay.update_translation(item[1], self.translator.backend, self.live.paused)
                elif kind == "shutdown":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    # ---------- 动作入口（热键/按钮/webui 共用，保持方法名兼容 webui.py） ----------

    def select_region_async(self):
        self.root.after(0, self.do_select_region)

    def set_fullscreen_async(self):
        self.root.after(0, self.do_capture_foreground)

    def trigger_now_async(self):
        self.live.capture_now()

    def vision_translate_async(self):
        self.screenshot.translate_now()

    def ask_ai_async(self, question, with_image=False, image_b64=None):
        self.screenshot.ask(question, with_image, image_b64)

    def toggle_pause(self):
        self.root.after(0, self._do_toggle_pause)

    def _do_toggle_pause(self):
        self.live.toggle_pause()
        self._set_stage(self._monitor_status_text())
        self.overlay.update_status(self.translator.backend, self.live.paused)
        self._push_state()

    # ---------- 捕获目标（F7 窗口 / F8 框选） ----------

    def _get_foreground_rect(self, hwnd=None):
        """获取游戏窗口客户区屏幕物理坐标 (x, y, w, h)。
        hwnd=None 取前台窗口（F7）；指定 hwnd 用于「选择窗口」列表。"""
        import ctypes
        if hwnd is None:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                logging.warning("F7 诊断: GetForegroundWindow 返回空（无前台窗口？）")
                return None

        info = window_info(hwnd)
        if info:
            target_elevated = process_elevated(info["pid"])
            logging.info(
                "F7 诊断: 窗口 标题[%s] 类[%s] pid=%d 本工具管理员=%s 目标管理员=%s",
                info["title"], info["cls"], info["pid"], self_elevated(), target_elevated,
            )
        else:
            target_elevated = None

        if hwnd in self.wm.exclude_hwnds():
            logging.info("F7 诊断: 前台是本工具自身窗口，已排除")
            return None

        rect = client_rect(hwnd)
        if rect is None:
            logging.warning("F7 诊断: 客户区不可用或过小，忽略")
            return None
        # UIPI：目标管理员权限而本工具普通权限 → 抓取会被系统静默拒绝
        if target_elevated and not self_elevated():
            logging.warning("F7 诊断: 游戏以管理员权限运行，本工具为普通权限，无法捕获窗口")
            self.status("游戏以管理员运行，本工具权限不足：请右键 GameTranslator.exe 以管理员身份运行")
            return None
        self._game_hwnd = hwnd
        return rect

    def do_capture_foreground(self, hwnd=None):
        """F7：捕获前台窗口（或指定窗口）客户区。进入游戏窗口默认关闭自动翻译。"""
        region = self._get_foreground_rect(hwnd)
        if not region:
            if hwnd is None:
                self.status("请在窗口选择器中点击游戏窗口")
            return
        self.config.region = region
        self.live.set_paused(True)  # 进入游戏窗口：默认仅手动触发（自动翻译按钮恢复）
        self.status(f"监控中 · 游戏窗口 {region}（自动翻译已关闭）")
        self.live.clear()
        self._set_stage("⏸ 自动翻译已关闭\n手动翻译或开自动翻译在输出面板")
        self.live.start(region, game_hwnd=self._game_hwnd)
        if self.wm.panel_shape == "lyrics":
            # 用户在歌词模式下捕获窗口：立即框定歌词覆盖区域并开始自动翻译
            self._pre_lyrics_region = region
            self._apply_lyrics_region()
            return
        if self.overlay_mode == "panel":
            self._do_toggle_overlay_mode(target="inplace")
        else:
            self.overlay.update_status(self.translator.backend, self.live.paused)
        self._push_state()

    @staticmethod
    def _region_text(region):
        return "全屏" if region == "fullscreen" else str(region)

    def do_select_region(self):
        region = select_region(self.root)
        if region:
            # 框选坐标是 tkinter 坐标系，统一换算为物理像素存储
            region_phys = (
                int(region[0] / self._dpi_fx), int(region[1] / self._dpi_fy),
                int(region[2] / self._dpi_fx), int(region[3] / self._dpi_fy),
            )
            self.config.region = region_phys
            self._game_hwnd = None  # 框选区域与游戏窗口不再对应，回退屏幕截图模式
            self.status(f"监控中 · 区域 {region_phys}")
            self._set_stage("● 监控中")
            self.live.clear()
            self.live.start(region_phys)
            self.overlay.update_status(self.translator.backend, self.live.paused)
            self._push_state()

    # ---------- 显示模式切换（F11）：inplace 覆盖原文 ↔ panel 独立面板 ----------

    _MODE_CYCLE = {"inplace": "panel", "panel": "inplace"}
    _MODE_LABEL = {"inplace": "覆盖原文", "panel": "独立面板"}

    def toggle_overlay_mode(self):
        self.root.after(0, self._do_toggle_overlay_mode)

    def _do_toggle_overlay_mode(self, target=None):
        self.overlay_mode = target or self._MODE_CYCLE[self.overlay_mode]
        self.config.set(self.overlay_mode, "overlay_mode")
        self.config.save()
        # 丢弃 UI 队列积压的旧绘制指令（重建 overlay 后会永久残留）
        while True:
            try:
                item = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if item and item[0] == "shutdown":
                self.ui_queue.put(item)
        try:
            self.overlay.win.destroy()
        except Exception:
            pass
        self.overlay = OverlayWindow(self.root, self.config, mode=self.overlay_mode)
        App.window_ids.append(self.overlay.win.winfo_id())
        self.overlay.update_status(self.translator.backend, self.live.paused)
        if self._overlay_hidden:
            self.overlay.set_visible(False)
        self.live.clear()  # 切换后强制重新翻译
        self._set_stage(f"显示: {self._MODE_LABEL[self.overlay_mode]}", "success")
        self._push_state()

    # ---------- 输出面板窗口 ----------

    def _toggle_output_window(self, force_hide=False):
        """显示/隐藏输出面板窗口。窗口常驻（隐藏保活），Vue 状态与历史不丢失。"""
        win = self.wm.get_win("panel")
        if not win:
            return
        try:
            if force_hide or self._output_open:
                win.hide()
                self._output_open = False
            else:
                self.wm.position_panel()
                win.show()
                self._output_open = True
                threading.Timer(0.3, lambda: self.wm.set_alpha(TITLE_PANEL, 0.4)).start()
            logging.info("输出面板%s", "显示" if self._output_open else "隐藏")
            self._bridge.push("output_state", {"open": self._output_open}, target="main")
        except Exception:
            logging.error("输出面板切换失败:\n%s", traceback.format_exc())

    def _set_panel_shape(self, shape):
        """输出面板形态切换（「歌词」tab 驱动）：lyrics 宽扁横条 / normal 常规。
        歌词模式：监控区域 = 歌词窗口覆盖的游戏区域（只翻译盖住的部分）。"""
        try:
            self.wm.set_panel_shape(shape)
            if shape == "lyrics":
                self._apply_lyrics_region()
            else:
                self._restore_region()
        except Exception:
            logging.error("面板形态切换失败:\n%s", traceback.format_exc())

    def _apply_lyrics_region(self, first_entry=False):
        """歌词模式：把监控区域切换为歌词窗口当前覆盖的屏幕矩形。
        依赖游戏窗口直抓（PrintWindow）——歌词面板浮在游戏上但不会入画；
        未捕获游戏窗口时提示先捕获（截屏模式会把歌词面板自己截进去）。"""
        if not self._game_hwnd:
            # 未捕获游戏窗口：歌词区域必须靠窗口直抓（截屏模式会把歌词面板自己截进去），
            # 自动弹出窗口选择器引导用户捕获
            self.status("歌词模式需先捕获游戏窗口（已为你打开选择器）")
            self._bridge.push("open_picker", None)
            return False
        if first_entry or self._pre_lyrics_region is None:
            self._pre_lyrics_region = self.config.region  # 记住进入前的区域
        # 歌词窗口屏幕矩形 → 游戏窗口客户区坐标（RegionMonitor 直抓客户区）
        prect = self.wm.panel_window_rect()
        grect = client_rect(self._game_hwnd)
        if not prect or not grect:
            return False
        gx, gy, gw, gh = grect
        px, py, pw, ph = prect
        # 求交集（歌词窗口可能部分移出游戏窗口）
        x1, y1 = max(px, gx), max(py, gy)
        x2, y2 = min(px + pw, gx + gw), min(py + ph, gy + gh)
        if x2 - x1 < 100 or y2 - y1 < 60:
            self.status("歌词窗口未覆盖到游戏画面，请拖到游戏文字区域")
            return False
        region = (x1 - gx, y1 - gy, x2 - x1, y2 - y1)  # 客户区相对坐标
        self.live.clear()
        self.live.start(region, game_hwnd=self._game_hwnd)
        # 歌词模式默认开启自动翻译（横条盖住对话行，来新对话自动出歌词）
        if self.live.paused:
            self.live.set_paused(False)
        self._push_state()
        return True

    def _restore_region(self):
        """离开歌词模式：恢复原监控区域。"""
        if self._pre_lyrics_region is not None:
            self.live.clear()
            self.live.start(self._pre_lyrics_region, game_hwnd=self._game_hwnd)
            self._pre_lyrics_region = None
            self._push_state()

    def _sync_lyrics_region(self):
        """歌词窗口移动/调整后重新框定（前端拖动结束/尺寸按钮触发）。"""
        if self.wm.panel_shape == "lyrics":
            self._apply_lyrics_region()

    def _adjust_lyrics(self, dw_ratio=0.0, dh=0):
        """调整歌词形态宽高并重新框定监控区域，尺寸存 config。"""
        shape = self.wm.adjust_lyrics(dw_ratio, dh)
        self.config.set(shape, "lyrics_shape")
        self.config.save()
        self._sync_lyrics_region()
        return shape

    # ---------- 生命周期 ----------

    def on_close(self):
        """统一清理后进程级退出（webview 隐藏窗 destroy 会死锁）。"""
        try:
            self.live.stop()
            self.config.save()
        except Exception:
            pass
        self.ui_queue.put(("shutdown",))
        os._exit(0)

    def run(self):
        if self.config.region:
            self.status(f"监控中 · {self._region_text(self.config.region)}")
            self.live.start(self.config.region, game_hwnd=self._game_hwnd)
        self.overlay.update_status(self.translator.backend, self.live.paused)
        self._push_state()
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

    win_main = webview.create_window(
        "游戏实时翻译", os.path.join(ui_dir, "index.html"), js_api=api,
        width=300, height=360, frameless=True, on_top=True,
        background_color="#14141f",
    )
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

    app.ui_queue.put(("shutdown",))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
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
            secrets.set_api_key("selftest-ok", section="selftest")
            ok = secrets.get_api_key(section="selftest") == "selftest-ok"
            secrets.delete_api_key(section="selftest")
            logging.info("凭据管理器: %s", "可用" if ok else "不可用")
        except Exception:
            logging.error("凭据管理器失败:\n%s", traceback.format_exc())
        logging.info("=== 自测结束，详见同目录 game-translator.log ===")
    else:
        main()
