"""翻译模块：Ollama 本地模型 / OpenAI 兼容云 LLM / DeepL 三后端切换 + 结果缓存 + 逐行并发翻译"""
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

# 整段翻译系统提示词
SYSTEM_PROMPT = (
    "You are a professional game localization translator. "
    "Translate the user's English game text into Simplified Chinese. "
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
        # 启用的后端从配置读取（config.json 的 enabled_backends）
        backends = config.get("enabled_backends", default=["llm", "deepl"])
        self.backends = [b for b in backends if b in ("ollama", "llm", "deepl")] or ["llm"]

        # 当前后端不在启用列表（如旧配置仍是 ollama）→ 回退到第一个可用后端
        if self.backend not in self.backends:
            config.set(self.backends[0], "backend")
            config.save()

        self._cache = {}          # 原文 -> 译文（整段与单行共用）
        self._cache_lock = threading.Lock()
        self._last_text = None    # 上一条原文（跳过重复）

    # ---------- 对外接口 ----------

    @property
    def backend(self):
        return self.config.get("backend", default="llm")

    def switch_backend(self):
        """切换到下一个后端，返回新后端名"""
        idx = self.backends.index(self.backend)
        new = self.backends[(idx + 1) % len(self.backends)]
        self.config.set(new, "backend")
        self.config.save()
        return new

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
            result = self._dispatch(text, SYSTEM_PROMPT)
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
                return self._dispatch(src, SYSTEM_PROMPT)
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

    # ---------- 后端实现 ----------

    def _dispatch(self, text, system=SYSTEM_PROMPT):
        backend = self.backend
        if backend == "ollama":
            return self._ollama(text, system)
        if backend == "llm":
            return self._llm(text, system)
        if backend == "deepl":
            return self._deepl([text])[0]
        raise RuntimeError(f"未知后端: {backend}")

    def _chat(self, url, headers, payload):
        resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def _ollama(self, text, system):
        url = self.config.get("ollama", "url", default="http://127.0.0.1:11434")
        model = self.config.get("ollama", "model", default="qwen2.5:7b")
        resp = requests.post(
            f"{url.rstrip('/')}/api/chat",
            json={
                "model": model,
                "stream": False,
                "options": {"temperature": 0.3},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()

    def _llm(self, text, system):
        """OpenAI 兼容接口（DeepSeek / GLM / 通义等）"""
        base_url = self.config.get("llm", "base_url", default="").rstrip("/")
        api_key = self.config.get("llm", "api_key", default="")
        model = self.config.get("llm", "model", default="")
        if not api_key:
            raise RuntimeError("未配置 llm.api_key（config.json）")
        return self._chat(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            payload={
                "model": model,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            },
        )

    def _deepl(self, texts):
        """DeepL 支持一次传多段文本，天然保持行对应"""
        api_key = self.config.get("deepl", "api_key", default="")
        if not api_key:
            raise RuntimeError("未配置 deepl.api_key（config.json）")
        host = "api-free.deepl.com" if api_key.endswith(":fx") else "api.deepl.com"
        data = []
        for t in texts:
            data.append(("text", t))
        resp = requests.post(
            f"https://{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            data=data + [("target_lang", "ZH")],
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return [item["text"].strip() for item in resp.json()["translations"]]
