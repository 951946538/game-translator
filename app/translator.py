"""翻译模块：OpenAI 兼容云 LLM（DeepSeek / GLM / 通义等）+ 结果缓存 + 逐行并发翻译"""
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

# 整段翻译系统提示词
SYSTEM_PROMPT = (
    "You are a professional game localization translator. "
    "Translate the user's game text into Simplified Chinese, regardless of the source "
    "language (English, Japanese, Korean, or any other). "
    "Text already in Simplified Chinese should be output as-is. "
    "Output ONLY the translation, no explanations. "
    "Keep game terminology natural and idiomatic. "
    "Keep the translation concise. "
    "Preserve line breaks of the original text."
)

REQUEST_TIMEOUT = 30
PARALLEL_WORKERS = 6  # 逐行并发翻译的线程数


class Translator:
    def __init__(self, config):
        self.config = config
        self._cache = {}          # 原文 -> 译文（整段与单行共用）
        self._cache_lock = threading.Lock()
        self._last_text = None    # 上一条原文（跳过重复）

    # ---------- 对外接口 ----------

    @property
    def backend(self):
        return "llm"

    def translate(self, text):
        """整段翻译并缓存；与上一条相同返回 None"""
        if not text:
            return None
        if text == self._last_text:
            return None

        with self._cache_lock:
            if text in self._cache:
                self._last_text = text
                return self._cache[text]

        try:
            result = self._llm(text, SYSTEM_PROMPT)
        except Exception as e:
            return f"[翻译失败: {e}]"

        self._remember(text, result)
        return result

    def translate_lines(self, lines):
        """
        逐行并发翻译（覆盖式翻译用）：返回与 lines 等长的译文列表。
        - 命中缓存的行直接返回（不重复请求）
        - 其余行并发翻译（总耗时≈单行耗时，比流式输出更快更稳）
        """
        out = [None] * len(lines)
        todo_idx, todo = [], []

        for i, ln in enumerate(lines):
            if not ln:
                out[i] = ""
                continue
            with self._cache_lock:
                if ln in self._cache:
                    out[i] = self._cache[ln]
            if out[i] is None:
                todo_idx.append(i)
                todo.append(ln)

        if not todo:
            return out

        def work(src):
            try:
                return self._llm(src, SYSTEM_PROMPT)
            except Exception as e:
                return f"[翻译失败: {e}]"

        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            results = list(pool.map(work, todo))

        for i, src, dst in zip(todo_idx, todo, results):
            out[i] = dst
            self._remember(src, dst)
        return out

    # ---------- 内部工具 ----------

    def _remember(self, src, dst):
        with self._cache_lock:
            if len(self._cache) > 500:
                self._cache.clear()
            self._cache[src] = dst
        self._last_text = src

    def _llm(self, text, system):
        """OpenAI 兼容接口（DeepSeek / GLM / 通义等）"""
        base_url = self.config.get("llm", "base_url", default="").rstrip("/")
        api_key = self.config.get_api_key("llm")
        model = self.config.get("llm", "model", default="")
        if not api_key:
            raise RuntimeError("未配置 llm.api_key（config.json）")
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
