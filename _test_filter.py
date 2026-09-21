import sys
sys.stdout.reconfigure(encoding="utf-8")
from main import _is_own_ui_text

cases = [
    ("立即翻译 (F6)", True),      # 按钮
    ("截图直译 (F5)", True),      # 按钮
    ("后端: llm (F10)", True),    # 后端按钮
    ("显示: 覆盖原文 (F11)", True),
    ("● 监控中", True),           # 状态
    ("F6立即翻译 F7窗口 F8选区 F9暂停 F10后端 F11显示", True),  # 状态栏
    ("● 监控中  |  后端: llm  |  F6立即翻译 F7窗口", True),
    ("Confirm", False),           # 游戏按钮
    ("Event Notice", False),      # 游戏标题
    ("翻译完成", True),           # 状态
    ("The translation is complete now", False),  # 游戏正文（含"translation"但不会全拆解）
]
ok = True
for text, expect in cases:
    got = _is_own_ui_text(text)
    mark = "✓" if got == expect else "✗"
    if got != expect:
        ok = False
    print(f"{mark} {text!r} → {got}（期望 {expect}）")
print("\n全部通过" if ok else "\n有失败用例")
