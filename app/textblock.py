"""OCR 行合并：把视觉上被换行拆开的行还原为逻辑文本块，保证翻译上下文完整"""


def group_lines(items, max_gap_ratio=0.6, x_overlap_min=0.25):
    """
    把 OCR 行合并为逻辑文本块。
    规则：垂直相邻（间距 < 行高 * max_gap_ratio）且水平范围有重叠的行属于同一段落。

    items: [{"box": [x1,y1,x2,y2], "text": str}, ...]
    返回: [{"box": [x1,y1,x2,y2], "text": 合并后文本, "line_h": 平均行高, "lines": 行数}, ...]
    """
    items = sorted(items, key=lambda it: (it["box"][1], it["box"][0]))
    blocks = []  # 内部用扁平键 x1/y1/x2/y2，最后统一导出 box

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

    # 导出统一格式并计算平均行高（用于字号自适应）
    result = []
    for b in blocks:
        result.append({
            "box": [b["x1"], b["y1"], b["x2"], b["y2"]],
            "text": b["text"],
            "lines": b["lines"],
            "line_h": max((b["y2"] - b["y1"]) / b["lines"], 12),
        })

    # 从上到下排序输出
    result.sort(key=lambda b: (b["box"][1], b["box"][0]))
    return result
