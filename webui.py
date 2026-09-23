"""Web UI 桥接层：PyApi 暴露给前端调用（JS → Python），EventBridge 推送事件（Python → JS）"""
import json
import queue
import threading
import time


class EventBridge:
    """Python → JS 事件推送：批量合并高频事件（流式 delta），evaluate_js 注入前端"""

    def __init__(self):
        self._q = queue.Queue()
        self._win = None
        self._thread = None

    def attach(self, win):
        """webview 窗口创建后绑定，并启动推送线程"""
        self._win = win
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def push(self, kind, data=None):
        self._q.put((kind, data))

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
            if self._win is None:
                continue
            try:
                payload = json.dumps(batch, ensure_ascii=False)
                self._win.evaluate_js(f"window.__pyEvents({payload})")
            except Exception:
                pass  # 窗口已关闭等场景


class PyApi:
    """暴露给前端 JS 的接口（pywebview js_api）。
    所有方法可能从 webview 主线程调用，App 侧方法需线程安全（现状即如此：热键线程也直接调用）。"""

    def __init__(self, app_provider, win_provider=None):
        self._app_provider = app_provider  # () -> App 实例
        self._win_provider = win_provider  # () -> pywebview 窗口

    def get_state(self):
        """前端初始化时拉取完整状态"""
        app = self._app_provider()
        region = app.config.region
        return {
            "paused": app.paused,
            "overlay_mode": app.overlay_mode,
            "region": list(region) if region and region != "fullscreen" else region,
            "status": getattr(app, "_status_text", ""),
            "has_api_key": bool(app.config.get_api_key("llm")),
            "base_url": app.config.get("llm", "base_url", default="https://api.deepseek.com/v1"),
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

    def ask(self, question, with_image=False):
        """问 AI（前端传参，替代原 tkinter 输入框）"""
        app = self._app_provider()
        question = (question or "").strip()
        if not question:
            return False
        threading.Thread(target=app._ask_worker, args=(question, bool(with_image)), daemon=True).start()
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

    # ---------- 无边框窗口控制（贴边收缩） ----------

    def close_window(self):
        self._win_provider().destroy()
        return True

    def minimize_window(self):
        self._win_provider().minimize()
        return True

    def expand_window(self):
        self._app_provider()._expand_window()
        return True

    def expand_panel(self, expanded):
        """主面板宽窄切换：True 展开（含右侧历史/提问区），False 收起（仅左列）"""
        self._app_provider()._expand_panel(bool(expanded))
        return True
