"""Web UI 桥接层：PyApi 暴露给前端调用（JS → Python），EventBridge 向多窗口推送事件（Python → JS）"""
import json
import queue
import threading
import time

from app.windows import TITLE_MAIN, TITLE_PANEL


class EventBridge:
    """Python → JS 事件推送：按路由表分发到各窗口，批量合并高频增量（流式 delta）"""

    # 事件路由表：主控窗(main) / 输出窗(panel)
    ROUTE = {
        "vision_delta": ("panel",),           # 流式增量只发输出窗（主窗不需要高频事件）
        "vision_done": ("panel",),
        "stage": ("main",),                   # 状态卡在主窗
        "state": ("main", "panel"),           # 暂停/显示模式：两窗都要（面板工具栏标签）
        "lyrics": ("panel",),                 # 歌词 tab（输出面板）
        "output_state": ("main",),
        # 其余事件（vision_start / ask_start / translation 等）默认发两窗：
        # 主窗亮红点，输出窗渲染内容
    }

    def __init__(self):
        self._q = queue.Queue()
        self._targets = {}  # name -> pywebview window
        self._thread = None

    def attach(self, name, win):
        """注册窗口（main=主控制窗 / panel=输出窗）"""
        self._targets[name] = win
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def push(self, kind, data=None, target=None):
        """target 显式指定时优先；否则查路由表；无表项默认发全部窗口"""
        self._q.put((kind, data, target))

    def _loop(self):
        while True:
            batch = [self._q.get()]
            # 合并 30ms 窗口内的事件（流式输出每次只推一小段，合并显著降低 JS 调用开销）
            deadline = time.time() + 0.03
            while time.time() < deadline:
                try:
                    batch.append(self._q.get(timeout=max(0.0, deadline - time.time())))
                except queue.Empty:
                    break
            by_target = {"main": [], "panel": []}
            for kind, data, target in batch:
                names = target or self.ROUTE.get(kind, ("main", "panel"))
                for n in names:
                    if n in by_target:
                        by_target[n].append([kind, data])
            for name, events in by_target.items():
                win = self._targets.get(name)
                if win is None or not events:
                    continue
                try:
                    payload = json.dumps(events, ensure_ascii=False)
                    win.evaluate_js(f"window.__pyEvents({payload})")
                except Exception:
                    pass  # 窗口已关闭等场景


class PyApi:
    """暴露给前端 JS 的接口（pywebview js_api）。
    所有方法可能从 webview 主线程调用，App 侧方法需线程安全（现状即如此：热键线程也直接调用）。"""

    def __init__(self, app_provider, wins=None):
        self._app_provider = app_provider  # () -> App 实例
        self._wins = wins or {}            # name -> pywebview window

    def get_state(self):
        """前端初始化时拉取完整状态"""
        app = self._app_provider()
        region = app.config.region
        return {
            "paused": app.live.paused,
            "overlay_mode": app.overlay_mode,
            "region": list(region) if region and region != "fullscreen" else region,
            "status": getattr(app, "_status_text", ""),
            "has_api_key": bool(app.config.get_api_key("llm")),
            "base_url": app.config.get("llm", "base_url", default="https://api.deepseek.com/v1"),
            "output_open": getattr(app, "_output_open", False),
        }

    def action(self, name):
        """按钮/热键统一入口：f5/f6/f7/f8/f9/f11"""
        app = self._app_provider()
        mapping = {
            "f5": app.vision_translate_async,
            "f6": app.trigger_now_async,
            "f7": app.set_fullscreen_async,
            "f8": app.select_region_async,
            "f9": app.toggle_pause,
            "f11": app.toggle_overlay_mode,
        }
        fn = mapping.get(name)
        if fn:
            fn()
            return True
        return False

    def ask(self, question, with_image=False, image_data=None):
        """问 AI。image_data = 引用的历史截图（data URI），优先于现场截图"""
        app = self._app_provider()
        question = (question or "").strip()
        if not question:
            return False
        app.ask_ai_async(question, bool(with_image), image_data or None)
        return True

    # ---------- 窗口选择器（绕开热键/前台限制的捕获入口） ----------

    def list_windows(self):
        """枚举当前打开的可见顶层窗口（排除本工具自身），供「选择窗口」列表展示"""
        import ctypes
        import ctypes.wintypes as wintypes
        app = self._app_provider()
        user32 = ctypes.windll.user32
        exclude = app.wm.exclude_hwnds()

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        result = []

        @WNDENUMPROC
        def cb(hwnd, _):
            try:
                if not hwnd or not user32.IsWindowVisible(hwnd) or hwnd in exclude:
                    return True
                # 工具窗/无边框辅助窗（输入体验等）不需要
                if user32.GetWindowLongW(hwnd, -20) & 0x80:  # WS_EX_TOOLWINDOW
                    return True
                rect = wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                w, h = rect.right - rect.left, rect.bottom - rect.top
                if w < 200 or h < 120:  # 最小化（坐标 -32000）或太小的窗口
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value.strip()
                if not title or title.startswith("Windows 输入体验"):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                result.append({"hwnd": int(hwnd), "title": title, "pid": int(pid.value)})
            except Exception:
                pass
            return True

        user32.EnumWindows(cb, 0)
        return result

    def capture_window(self, hwnd):
        """捕获指定窗口（「选择窗口」列表点选后调用）"""
        app = self._app_provider()
        try:
            hwnd = int(hwnd)
        except (TypeError, ValueError):
            return False
        app.root.after(0, lambda: app.do_capture_foreground(hwnd=hwnd))
        return True

    def save_api_key(self, base_url, key):
        """保存 API 设置：地址进 config.json，密钥进 Windows 凭据管理器"""
        app = self._app_provider()
        key = (key or "").strip()
        if not key:
            return {"ok": False, "msg": "密钥不能为空"}
        if (base_url or "").strip():
            app.config.set(base_url.strip(), "llm", "base_url")
        app.config.set_api_key(key, "llm")
        app.config.save()
        app._status("API 密钥已保存到 Windows 凭据管理器")
        return {"ok": True, "msg": "已保存"}

    def open_url(self, url):
        import webbrowser
        webbrowser.open(url)
        return True

    # ---------- 窗口控制 ----------

    def close_window(self):
        """关闭主控窗 = 退出程序。
        做完必要清理后进程级退出（os._exit）：pywebview 对 hidden 窗口的
        destroy 不可靠（会死锁），webview.start() 也会因隐藏窗口永不返回。"""
        import os
        app = self._app_provider()
        try:
            app.config.save()
            app.stop_monitor()
            if getattr(app, "_hotkeys", None):
                app._hotkeys.stop()
        except Exception:
            pass
        os._exit(0)

    def minimize_window(self):
        self._wins["main"].minimize()
        return True

    def toggle_output(self):
        """显示/隐藏输出面板（第二窗口，固定尺寸无 resize）"""
        app = self._app_provider()
        app._toggle_output_window()
        return True

    def close_output(self):
        """输出面板标题栏关闭按钮 = 隐藏（保留状态）"""
        app = self._app_provider()
        app._toggle_output_window(force_hide=True)
        return True

    def minimize_output(self):
        """输出面板最小化（窗口仍在任务栏，状态保留）"""
        win = self._wins.get("panel")
        if win:
            win.minimize()
        return True

    def set_output_alpha(self, alpha):
        """输出面板透明度：悬停时前端调 1.0（不透明），移开恢复 0.4"""
        app = self._app_provider()
        app.wm.set_alpha(TITLE_PANEL, float(alpha))
        return True

    def set_main_alpha(self, alpha):
        """主控窗透明度：悬停 1.0，移开 0.4"""
        app = self._app_provider()
        app.wm.set_alpha(TITLE_MAIN, float(alpha), hwnd=app.wm.main_hwnd)
        return True

    def set_panel_shape(self, shape):
        """输出面板形态：'lyrics'（桌面歌词横条）/ 'normal'（常规面板），由歌词 tab 驱动"""
        self._app_provider()._set_panel_shape(str(shape))
        return True

    def toggle_overlay_visible(self):
        """显示/隐藏覆盖层译文（实时翻译 tab 工具栏开关）"""
        self._app_provider()._toggle_overlay_visible()
        return True
