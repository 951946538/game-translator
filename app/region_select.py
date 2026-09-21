"""全屏框选区域：半透明遮罩 + 橡皮筋选框，返回 (x, y, w, h)"""


def select_region(root):
    """阻塞式选择屏幕区域，Esc 取消返回 None"""
    import tkinter as tk

    result = {"region": None}

    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.attributes("-topmost", True)
    top.attributes("-alpha", 0.3)
    top.configure(bg="black")
    top.geometry(f"{top.winfo_screenwidth()}x{top.winfo_screenheight()}+0+0")

    canvas = tk.Canvas(top, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.configure(cursor="crosshair")

    tip = canvas.create_text(
        top.winfo_screenwidth() // 2, 40,
        text="拖动鼠标框选游戏文本区域，按 Esc 取消",
        fill="#ffffff", font=("Microsoft YaHei UI", 14),
    )
    rect_id = [None]
    start = [None]

    def on_press(e):
        start[0] = (e.x_root, e.y_root)
        if rect_id[0]:
            canvas.delete(rect_id[0])

    def on_drag(e):
        if rect_id[0]:
            canvas.delete(rect_id[0])
        x0, y0 = start[0]
        rect_id[0] = canvas.create_rectangle(
            min(x0, e.x_root), min(y0, e.y_root), max(x0, e.x_root), max(y0, e.y_root),
            outline="#4f8cff", width=2,
        )

    def on_release(e):
        x0, y0 = start[0]
        x1, y1 = e.x_root, e.y_root
        region = (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        if region[2] > 30 and region[3] > 15:  # 过小的选区视为误操作
            result["region"] = region
        top.destroy()

    def on_esc(_e):
        top.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    top.bind("<Escape>", on_esc)
    top.focus_force()

    top.wait_window()
    return result["region"]
