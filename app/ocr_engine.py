"""PaddleOCR 封装（英文识别，优先精准）"""
import os

# paddlepaddle 3.3 + paddleocr 3.7 在 CPU 推理存在 PIR/mkldnn 不兼容问题，必须关闭
# （报错：ConvertPirAttribute2RuntimeAttribute not support）
os.environ.setdefault("FLAGS_use_mkldnn", "false")


class OCREngine:
    def __init__(self, lang="en"):
        from paddleocr import PaddleOCR

        # paddleocr 3.x 支持 enable_mkldnn 参数；2.x 走旧参数
        try:
            self.ocr = PaddleOCR(lang=lang, enable_mkldnn=False)
        except TypeError:
            try:
                self.ocr = PaddleOCR(lang=lang, use_angle_cls=False, show_log=False)
            except TypeError:
                self.ocr = PaddleOCR(lang=lang)

    def extract(self, frame_rgb):
        """
        识别一帧画面（RGB numpy 数组），返回按行拼接的文本，无文本返回 ""
        """
        import cv2
        import numpy as np

        img_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        texts = []

        try:
            # paddleocr 3.x：predict 返回 Result 列表，rec_texts 为识别文本
            for r in self.ocr.predict(img_bgr):
                if hasattr(r, "get"):
                    texts.extend(r.get("rec_texts", []))
                else:
                    texts.extend(list(getattr(r, "rec_texts", [])))
        except AttributeError:
            # paddleocr 2.x：ocr 返回 [[box, (text, conf)], ...]
            result = self.ocr.ocr(img_bgr, cls=False)
            if result:
                for block in result:
                    if not block:
                        continue
                    for line in block:
                        texts.append(line[1][0])

        return "\n".join(t.strip() for t in texts if t and t.strip())
