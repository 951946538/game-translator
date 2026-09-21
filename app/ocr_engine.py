"""PaddleOCR 封装：返回带坐标的识别结果，支持覆盖式翻译；OCR 前降采样提速"""
import os

# paddlepaddle 3.3 + paddleocr 3.7 在 CPU 推理存在 PIR/mkldnn 不兼容问题，必须关闭
os.environ.setdefault("FLAGS_use_mkldnn", "false")

# OCR 输入最大宽度：超过则降采样提速。
# 高分屏（如 3800x2000）压到 1280 会让小字糊掉导致识别错乱，1920 是精度/速度的平衡点
MAX_OCR_WIDTH = 1920


class OCREngine:
    def __init__(self, lang="en", min_score=0.6, high_accuracy=True):
        from paddleocr import PaddleOCR

        self.min_score = min_score  # 置信度过滤：低于该分数的识别结果丢弃（过滤艺术字体噪声）

        # 屏幕文本不需要文档方向分类/弯曲矫正/行方向分类，关闭后少加载 3 个模型、速度大增
        base_kwargs = dict(
            enable_mkldnn=False,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )

        if high_accuracy:
            # 高精度 server 模型（首次使用需下载约 100MB，识别明显更准）。
            # 指定模型名时 lang/ocr_version 会被忽略，不传以避免警告
            try:
                self.ocr = PaddleOCR(
                    text_detection_model_name="PP-OCRv5_server_det",
                    text_recognition_model_name="PP-OCRv5_server_rec",
                    **base_kwargs,
                )
                return
            except (TypeError, ValueError):
                pass  # 模型名不可用则回退默认

        try:
            self.ocr = PaddleOCR(lang=lang, **base_kwargs)
        except TypeError:
            try:
                self.ocr = PaddleOCR(lang=lang, use_angle_cls=False, show_log=False)
            except TypeError:
                self.ocr = PaddleOCR(lang=lang)

    @staticmethod
    def _accept(text, score, min_score):
        """噪声过滤：低置信度、纯符号、超短纯字母数字的结果丢弃"""
        if score is not None and score < min_score:
            return False
        t = text.strip()
        if not t:
            return False
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in t)
        has_alnum = any(ch.isalnum() for ch in t)
        if not has_alnum and not has_cjk:
            return False  # 纯符号/标点
        if len(t) <= 2 and not has_cjk:
            return False  # 超短纯字母数字（地图装饰字母等噪声）
        return True

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
                # rec_scores 缺失时补 None，避免 zip 截断导致全部结果丢失
                scores = r.get("rec_scores", None) if hasattr(r, "get") else getattr(r, "rec_scores", None)
                if scores is None:
                    scores = [None] * len(texts)
                for t, poly, score in zip(texts, polys, scores):
                    t = str(t).strip()
                    if not self._accept(t, score, self.min_score):
                        continue
                    pts = np.asarray(poly)
                    x1, y1 = pts.min(axis=0)
                    x2, y2 = pts.max(axis=0)
                    results.append({
                        "box": [int(x1 * scale), int(y1 * scale), int(x2 * scale), int(y2 * scale)],
                        "text": t,
                    })
        except AttributeError:
            # paddleocr 2.x：ocr 返回 [[box, (text, conf)], ...]
            results = []  # 清空可能的部分结果，避免重复
            raw = self.ocr.ocr(img_bgr, cls=False)
            if raw:
                for block in raw:
                    if not block:
                        continue
                    for box, (t, conf) in block:
                        t = str(t).strip()
                        if not self._accept(t, conf, self.min_score):
                            continue
                        xs = [p[0] for p in box]
                        ys = [p[1] for p in box]
                        results.append({
                            "box": [int(min(xs) * scale), int(min(ys) * scale),
                                    int(max(xs) * scale), int(max(ys) * scale)],
                            "text": t,
                        })

        return [r for r in results if r["text"]]

    def extract(self, frame_rgb):
        """识别并返回按行拼接的纯文本（面板模式用）"""
        items = self.extract_detail(frame_rgb)
        return "\n".join(item["text"] for item in items)
