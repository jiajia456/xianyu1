# -*- coding: utf-8 -*-
"""离线验证：图标双击不生效（弹窗不出、图标还在）时，是否最多重复到 3 次。"""
import sys, types
import numpy as np

sys.path.insert(0, r"C:\Users\Administrator\Desktop\我在峡谷打\kk_auto")
import watch_early as we

# 复刻 main() 里的模板初始化（cycle 依赖这些全局尺寸常量）
_t_icon = we.load("templates/btn_early_challenge.png")
we.TPL_ICON = _t_icon
we.TPL_ICON_H, we.TPL_ICON_W = _t_icon.shape[:2]
_t_yes = we.load("templates/btn_popup_yes.png")
we.TPL_YES = _t_yes
we.TPL_YES_H, we.TPL_YES_W = _t_yes.shape[:2]


class FakeInp:
    def __init__(self):
        self.clicks = []

    def move(self, *a):
        pass

    def click(self, *a, **k):
        self.clicks.append(("click", k.get("clicks", 1)))

    def click_hover(self, x, y, hover=0, hold=0, clicks=1, gap=0):
        self.clicks.append(("hover", clicks))


class W:
    hwnd = 1


def run_case(icon_visible_checks, popup_ever):
    """icon_visible_checks: 重试前查图标时「图标在」的次数上限（None=一直在）。"""
    inp = FakeInp()
    state = {"check": 0}
    we.ig.grab_checked = lambda w: np.zeros((900, 1200, 3), np.uint8)
    we.ig.grab = lambda w: np.zeros((900, 1200, 3), np.uint8)
    we.find_popup = lambda img: (popup_ever and state["check"] >= 1, 0.0, (0, 0))
    we.find_save = lambda img: (False, 0.0, (0, 0))

    def fake_find_icon(img):
        return (state["check"] < icon_visible_checks, 1.0, (100, 100), (200, 150))

    we.find_icon = fake_find_icon

    # 每次双击后 state["check"] += 1 模拟「图标状态可能变化」
    orig_ch = inp.click_hover

    def ch(x, y, hover=0, hold=0, clicks=1, gap=0):
        orig_ch(x, y, hover, hold, clicks, gap)
        state["check"] += 1

    inp.click_hover = ch

    msgs = []
    orig_log = we.log
    we.log = lambda m: msgs.append(m)
    args = types.SimpleNamespace(dry=False, shot=False, no_auto_guard=True,
                                 no_slots=True, slots_rounds=2, slots_gap=1.0)
    r = we.cycle(W(), inp, args)
    we.log = orig_log
    hovers = [c for c in inp.clicks if c[0] == "hover"]
    for m in msgs:
        print("   LOG:", m)
    return r, hovers


print("== 用例1：弹窗始终不出、图标一直在（应双击 3 次） ==")
r, h = run_case(999, False)
print("结果:", r, "| 双击次数:", len(h), "| 每次下数:", [x[1] for x in h])

print()
print("== 用例2：第一次双击后图标消失（应只双击 1 次） ==")
r, h = run_case(1, False)
print("结果:", r, "| 双击次数:", len(h))

print()
print("== 用例3：第一次就出弹窗（应只双击 1 次，且走点「是」流程） ==")
r, h = run_case(999, True)
print("结果:", r, "| 双击次数:", len(h))
