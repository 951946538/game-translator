"""实测极简中文提示词：英文截图应输出简体中文"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
from PIL import Image, ImageDraw

from app.config import Config
from app import vision

img = Image.new("RGB", (1280, 720), (24, 24, 38))
d = ImageDraw.Draw(img)
d.text((80, 80), "Mission Complete!", fill="white")
d.text((80, 130), "Reward: 500 Gold, Sword of Dawn x1", fill="white")
d.text((80, 180), "Continue to the next chapter", fill="white")
d.rectangle([80, 580, 240, 640], outline=(140, 140, 160), width=3)
d.text((120, 598), "Accept", fill="white")

t0 = time.time()
content = ""
for kind, chunk in vision.translate_screenshot_stream(np.array(img), Config()):
    if kind == "content":
        content += chunk
print(f"耗时 {time.time()-t0:.1f}s")
print("--- 输出 ---")
print(content)
cn = sum('\u4e00' <= c <= '\u9fff' for c in content)
en = sum(c.isascii() and c.isalpha() for c in content)
print(f"--- 中文 {cn} 字 / 英文字母 {en} 个 ---")
