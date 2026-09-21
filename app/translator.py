"""翻译模块：Ollama 本地模型 / OpenAI 兼容云 LLM / DeepL 三后端切换 + 结果缓存"""
import threading

import requests

# 游戏本地化系统提示词
SYSTEM_PROMPT = (
    "You are a professional game localization translator. "
    "Translate the user's English game text into Simplified Chinese. "
    "Output ONLY the translation, no explanations. "
    "Keep game terminology natural and idiomatic. "
    "Preserve line breaks of the original text."
)

REQUEST_TIMEOUT = 30


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

        self._cache = {}          # 原文 -> 译文
        self._cache_lock = threading.Lock()
        self._last_text = None    # 上一条原文（跳过重复）

    # ---------- 对外接口 ----------

    @property
    def backend(self):
        return self.config.get("backend", default="ollama")

    def switch_backend(self):
        """切换到下一个后端，返回新后端名"""
        idx = self.backends.index(self.backend)
        new = self.backends[(idx + 1) % len(self.backends)]
        self.config.set(new, "backend")
        self.config.save()
        return new

    def translate(self, text):
        """翻译并缓存；相同原文直接命中缓存。失败时返回 None 并附带错误信息前缀"""
        if not text:
            return None
        if text == self._last_text:
            return None  # 与上一条相同，跳过

        with self._cache_lock:
            if text in self._cache:
                self._last_text = text
                return self._cache[text]

        try:
            result = self._dispatch(text)
        except Exception as e:
            return f"[翻译失败: {e}]"

        with self._cache_lock:
            if len(self._cache) > 500:  # 缓存上限，防止内存膨胀
                self._cache.clear()
            self._cache[text] = result
        self._last_text = text
        return result

    # ---------- 后端实现 ----------

    def _dispatch(self, text):
        backend = self.backend
        if backend == "ollama":
            return self._ollama(text)
        if backend == "llm":
            return self._llm(text)
        if backend == "deepl":
            return self._deepl(text)
        raise RuntimeError(f"未知后端: {backend}")

    def _ollama(self, text):
        url = self.config.get("ollama", "url", default="http://127.0.0.1:11434")
        model = self.config.get("ollama", "model", default="qwen2.5:7b")
        resp = requests.post(
            f"{url.rstrip('/')}/api/chat",
            json={
                "model": model,
                "stream": False,
                "options": {"temperature": 0.3},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()

    def _llm(self, text):
        """OpenAI 兼容接口（DeepSeek / GLM / 通义等）"""
        base_url = self.config.get("llm", "base_url", default="").rstrip("/")
        api_key = self.config.get("llm", "api_key", default="")
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
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def _deepl(self, text):
        api_key = self.config.get("deepl", "api_key", default="")
        if not api_key:
            raise RuntimeError("未配置 deepl.api_key（config.json）")
        # 免费版密钥以 :fx 结尾走 api-free 域名，否则走 pro
        host = "api-free.deepl.com" if api_key.endswith(":fx") else "api.deepl.com"
        resp = requests.post(
            f"https://{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            data={"text": text, "target_lang": "ZH"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["translations"][0]["text"].strip()
