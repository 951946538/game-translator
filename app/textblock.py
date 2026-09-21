"""OCR 行合并：优先按游戏文本框（矩形面板）归组，无面板时回退到行距启发式合并"""
import cv2
import numpy as np


def detect_panels(frame_rgb, min_area_ratio=0.008, max_area_ratio=0.95):
    """
    检测疑似文本框面板的矩形区域（游戏对话框通常有明显的边框）。
    返回 [(x1, y1, x2, y2), ...]
    """
    h, w = frame_rgb.shape[:2]
    gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)

    # 边缘检测 + 形态学闭合：把断续的边框线连成完整矩形轮廓
    edges = cv2.Canny(gray, 60, 180)
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    panels = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cw * ch
        if area < min_area_ratio * w * h or area > max_area_ratio * w * h:
            continue
        if cw < 80 or ch < 40:
            continue
        # 矩形度：轮廓面积/外接矩形面积，过滤复杂形状
        if cv2.contourArea(c) < 0.6 * area:
            continue
        panels.append((x, y, x + cw, y + ch))
    return panels


def group_lines(items, frame=None, max_gap_ratio=0.6, x_overlap_min=0.25):
    """
    把 OCR 行合并为逻辑文本块。
    优先策略：frame 提供时先检测矩形面板，行按所在面板归组（同面板才算同段，
    避免相邻面板误合并）；面板内部及无面板的行回退到行距启发式。

    items: [{"box": [x1,y1,x2,y2], "text": str}, ...]（与 frame 同坐标系）
    返回: [{"box": [x1,y1,x2,y2], "text": 合并后文本, "line_h": 平均行高, "lines": 行数}, ...]
    """
    if frame is not None:
        panels = detect_panels(frame)
        if panels:
            return _group_by_panels(items, panels)
    return _group_by_gap(items, max_gap_ratio, x_overlap_min)


def _group_by_panels(items, panels):
    """行按最小包含面板归组；面板内/面板外的行各自再用行距合并"""
    assigned = {}
    loose = []
    for it in items:
        x1, y1, x2, y2 = it["box"]
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        best, best_area = None, None
        for i, (px1, py1, px2, py2) in enumerate(panels):
            if px1 <= cx <= px2 and py1 <= cy <= py2:
                a = (px2 - px1) * (py2 - py1)
                if best is None or a < best_area:
                    best, best_area = i, a
        if best is None:
            loose.append(it)
        else:
            assigned.setdefault(best, []).append(it)

    blocks = []
    for lines in assigned.values():
        # 面板内部更宽松地合并：同一个文本框内的行几乎必然属于同一段
        blocks.extend(_group_by_gap(lines, max_gap_ratio=1.2, x_overlap_min=0.1))
    blocks.extend(_group_by_gap(loose))
    blocks.sort(key=lambda b: (b["box"][1], b["box"][0]))
    return blocks


def _group_by_gap(items, max_gap_ratio=0.6, x_overlap_min=0.25):
    """行距启发式合并：垂直相邻且水平有重叠的行属于同段"""
    items = sorted(items, key=lambda it: (it["box"][1], it["box"][0]))
    blocks = []  # 内部用扁平键，最后统一导出 box

    for it in items:
        x1, y1, x2, y2 = it["box"]
        h = max(y2 - y1, 8)
        text = it["text"]

        merged = False
        for b in blocks:
            gap = y1 - b["y2"]
            # 垂直间距在合理范围内（允许行框轻微重叠）
            if not (-h * 0.4 < gap < h * max_gap_ratio):
                continue
            # 水平范围有重叠（同一段落的行通常对齐或交叠）
            x_overlap = min(x2, b["x2"]) - max(x1, b["x1"])
            min_width = min(x2 - x1, b["x2"] - b["x1"])
            if x_overlap < min_width * x_overlap_min:
                continue

            # 合并进该块；处理英文连字符换行（soft-soft → softsoft）
            if b["text"].endswith("-"):
                b["text"] = b["text"][:-1] + text
            else:
                b["text"] = b["text"] + " " + text
            b["x1"] = min(b["x1"], x1)
            b["y1"] = min(b["y1"], y1)
            b["x2"] = max(b["x2"], x2)
            b["y2"] = max(b["y2"], y2)
            b["lines"] += 1
            merged = True
            break

        if not merged:
            blocks.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "text": text, "lines": 1})

    result = []
    for b in blocks:
        result.append({
            "box": [b["x1"], b["y1"], b["x2"], b["y2"]],
            "text": b["text"],
            "lines": b["lines"],
            "line_h": max((b["y2"] - b["y1"]) / b["lines"], 12),
        })
    result.sort(key=lambda b: (b["box"][1], b["box"][0]))
    return result
