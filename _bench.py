"""OCR 基准测试：测量当前后端的识别耗时"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
from PIL import Image, ImageDraw

from app.config import Config
from app.ocr_engine import OCREngine

# 生成 1920 宽测试图（模拟典型裁剪区域）
img = Image.new("RGB", (1920, 1080), (30, 30, 40))
d = ImageDraw.Draw(img)
for i in range(20):
    d.text((100, 60 + i * 50), f"Line {i}: The ancient sword lies hidden in the dark forest number {i}.", fill="white")

frame = np.array(img)

print("初始化 OCR 引擎…")
t0 = time.time()
engine = OCREngine(lang="en", min_score=0.6, high_accuracy=False)
print(f"引擎初始化: {time.time()-t0:.1f}s")

# 预热一次（首次推理含图优化等开销）
engine.extract_detail(frame)

t0 = time.time()
items = engine.extract_detail(frame)
dt = time.time() - t0
print(f"识别耗时: {dt:.2f}s（{len(items)} 行，1920x1080）")

import paddle
print(f"paddle 版本: {paddle.__version__}")
try:
    print(f"GPU 可用: {paddle.device.is_compiled_with_cuda()}")
except Exception:
    pass
