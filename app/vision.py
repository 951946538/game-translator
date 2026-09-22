"""视觉/对话模块：截图直译 + 直接问 AI（可选附带游戏画面），全部流式输出。"""
import base64
import json
import logging
import traceback

import requests

VISION_PROMPT = "帮我翻译图片中的游戏文本。按阅读顺序（从上到下、从左到右）逐行输出简体中文译文，只输出译文，不要任何解释。"

REQUEST_TIMEOUT = (15, 180)  # (连接, 读取) —— 流式生成可能持续较久


def encode_screenshot(frame_rgb, max_width=2048, quality=82):
    """压缩截图为 JPEG base64（4K 原图过大，控制在几百 KB）"""
    import cv2
    h, w = frame_rgb.shape[:2]
    if w > max_width:
        scale = max_width / w
        frame_rgb = cv2.resize(frame_rgb, (max_width, int(h * scale)))
    ok, buf = cv2.imencode(".jpg", frame_rgb, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("截图编码失败")
    return base64.b64encode(buf).decode("ascii")


def _stream_chat(model, messages, cfg, section="llm"):
    """通用流式对话：逐块 yield ("reasoning"|"content", 增量文本)"""
    base_url = cfg.get(section, "base_url", default="") or cfg.get("llm", "base_url", default="")
    api_key = cfg.get(section, "api_key", default="") or cfg.get("llm", "api_key", default="")
    if not api_key:
        raise RuntimeError("未配置 api_key（config.json 的 llm 或 vision 段）")

    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "temperature": 0.3, "stream": True, "messages": messages},
        stream=True,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()

    # SSE 流解析：data: {...} / data: [DONE]
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
        rc = delta.get("reasoning_content")
        if rc:
            yield ("reasoning", rc)
        c = delta.get("content")
        if c:
            yield ("content", c)


def translate_screenshot_stream(frame_rgb, cfg):
    """流式截图直译：整张截图发给视觉模型按阅读顺序翻译"""
    b64 = encode_screenshot(frame_rgb)
    model = cfg.get("vision", "model", default="deepseek-v4-flash-vision-exp")
    yield from _stream_chat(
        model,
        [{"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        cfg, section="vision",
    )


def ask_stream(question, frame_rgb, cfg, with_image=False):
    """流式问 AI：with_image=True 时把当前游戏截图一并交给视觉模型，
    可回答"画面里这个按钮是什么"这类问题；否则纯文本走 llm 模型。"""
    if with_image and frame_rgb is not None:
        model = cfg.get("vision", "model", default="deepseek-v4-flash-vision-exp")
        b64 = encode_screenshot(frame_rgb)
        content = [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
        yield from _stream_chat(
            model, [{"role": "user", "content": content}], cfg, section="vision",
        )
    else:
        model = cfg.get("llm", "model", default="deepseek-chat")
        yield from _stream_chat(
            model, [{"role": "user", "content": question}], cfg, section="llm",
        )
