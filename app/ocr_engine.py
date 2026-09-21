"""PaddleOCR 封装（英文识别，优先精准）"""
import numpy as np


class OCREngine:
    def __init__(self, lang="en"):
        from paddleocr import PaddleOCR

        # 兼容 paddleocr 2.x / 3.x 参数差异
        try:
            self.ocr = PaddleOCR(lang=lang, use_angle_cls=False, show_log=False)
        except (TypeError, ValueError):
            try:
                self.ocr = PaddleOCR(lang=lang, use_angle_cls=False)
            except TypeError:
                self.ocr = PaddleOCR(lang=lang)
        self._ready = True

    def extract(self, frame_rgb):
        """
        识别一帧画面（RGB numpy 数组），返回按阅读顺序拼接的文本
        空文本返回 ""
        """
        import cv2
        img_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

        # 兼容不同版本调用方式
        try:
            result = self.ocr.ocr(img_bgr, cls=False)
        except TypeError:
            try:
                result = self.ocr.ocr(img_bgr)
            except AttributeError:
                result = self.ocr.predict(img_bgr)

        texts = []
        try:
            if result:
                for block in result:
                    if not block:
                        continue
                    for line in block:
                        # 2.x 结构: [box, (text, conf)]
                        texts.append(line[1][0])
        except (TypeError, IndexError):
            # 3.x predict 返回 dict 列表
            for item in result if isinstance(result, list) else []:
                texts.extend(item.get("rec_texts", []))

        # 按行拼接（从上到下）
        return "\n".join(t.strip() for t in texts if t and t.strip())
