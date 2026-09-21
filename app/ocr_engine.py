"""PaddleOCR 封装：返回带坐标的识别结果，支持覆盖式翻译；OCR 前降采样提速"""
import os

# paddlepaddle 3.3 + paddleocr 3.7 在 CPU 推理存在 PIR/mkldnn 不兼容问题，必须关闭
os.environ.setdefault("FLAGS_use_mkldnn", "false")

# OCR 输入最大宽度（超过则降采样，速度提升约 3 倍，识别率几乎无损）
MAX_OCR_WIDTH = 1280


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

    def extract_detail(self, frame_rgb):
        """
        识别一帧画面（RGB numpy 数组）。
        返回 [{"box": [x1, y1, x2, y2], "text": "..."}, ...]
        box 坐标已还原为原始屏幕坐标。
        """
        import cv2
        import numpy as np

        h, w = frame_rgb.shape[:2]
        scale = w / MAX_OCR_WIDTH if w > MAX_OCR_WIDTH else 1.0
        if scale > 1.0:
            work = cv2.resize(frame_rgb, (MAX_OCR_WIDTH, int(round(h / scale))))
        else:
            work = frame_rgb

        img_bgr = cv2.cvtColor(work, cv2.COLOR_RGB2BGR)
        results = []

        try:
            # paddleocr 3.x：predict 返回 Result 列表
            for r in self.ocr.predict(img_bgr):
                texts = r.get("rec_texts", []) if hasattr(r, "get") else list(getattr(r, "rec_texts", []))
                polys = r.get("rec_polys", []) if hasattr(r, "get") else list(getattr(r, "rec_polys", []))
                for t, poly in zip(texts, polys):
                    pts = np.asarray(poly)
                    x1, y1 = pts.min(axis=0)
                    x2, y2 = pts.max(axis=0)
                    results.append({
                        "box": [int(x1 * scale), int(y1 * scale), int(x2 * scale), int(y2 * scale)],
                        "text": str(t).strip(),
                    })
        except AttributeError:
            # paddleocr 2.x：ocr 返回 [[box, (text, conf)], ...]
            raw = self.ocr.ocr(img_bgr, cls=False)
            if raw:
                for block in raw:
                    if not block:
                        continue
                    for box, (t, _conf) in block:
                        xs = [p[0] for p in box]
                        ys = [p[1] for p in box]
                        results.append({
                            "box": [int(min(xs) * scale), int(min(ys) * scale),
                                    int(max(xs) * scale), int(max(ys) * scale)],
                            "text": str(t).strip(),
                        })

        return [r for r in results if r["text"]]

    def extract(self, frame_rgb):
        """识别并返回按行拼接的纯文本（面板模式用）"""
        items = self.extract_detail(frame_rgb)
        return "\n".join(item["text"] for item in items)
