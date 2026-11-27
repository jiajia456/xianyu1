# -*- coding: utf-8 -*-
"""离线验证：秘境挑战的点击已统一为「双击 + 格子原地重试」。"""
import sys, types
import numpy as np

sys.path.insert(0, r"C:\Users\Administrator\Desktop\我在峡谷打\kk_auto")
import secret_challenge as sc


class FakeInp:
    def __init__(self):
        self.calls = []

    def click(self, x, y, button="left", hold=0.05, clicks=1, gap=0.05):
        self.calls.append(("click", x, y, clicks, gap))

    def click_hover(self, x, y, hover=0.8, hold=0.12, button="left", clicks=1, gap=0.06):
        self.calls.append(("hover", x, y, clicks, gap, hover))

    def key(self, k):
        self.calls.append(("key", k))


class W:
    hwnd = 1


CFG = {
    "click_hold": 0.09,
    "click_clicks": 2,
    "click_gap": 0.12,
    "retry": 4,
    "wait_dialog": 0.0,
    "panel_settle": 0.0,
    "icon_hover": 0.0,
    "icon_retries": 2,
    "icon_retry_gap": 0.0,
    "npc_hover": 0.0,
    "yes_xy": [762, 466],
    "no_xy": [919, 463],
    "options": {
        "island": {"label": "海岛挑战", "xy": [1373, 743],
                   "dialog_score": 0.72, "dialog_text": "是否开启海岛秘境挑战?"},
    },
}

print("== 用例1：_click 默认双击 ==")
inp = FakeInp()
sc._click(inp, CFG, 100, 200)
sc._click(inp, CFG, 300, 400, hover=1.2)
for c in inp.calls:
    print("   ", c)

print()
print("== 用例2：点格子弹窗第 1 次不出 → 原地重试 1 次 ==")
seq = {"n": 0}


def fake_wait_dialog(w, cfg, key, timeout):
    seq["n"] += 1
    return (seq["n"] >= 2, 0.5 if seq["n"] < 2 else 0.95)


sc.open_panel = lambda w, inp, cfg, verbose=True: (0, 0, True)
sc.panel_secret_open = lambda img, cfg=None, threshold=None, extra_keys=(): (
    True, {"island": (1.0, (1373, 743)), "undead": (1.0, (1449, 743)), "threshold": 0.72})
sc.ig.grab_checked = lambda w: np.zeros((900, 1600, 3), np.uint8)
sc.dialog_score = lambda img, opt: 0.0
sc.wait_dialog = fake_wait_dialog
sc._scale_xy = lambda w, xy: tuple(xy)

inp2 = FakeInp()
import time as _t
_t.sleep = lambda *a, **k: None  # 跳过等待，测试跑得更快

res = sc.do_option(W(), inp2, CFG, "island", "yes")
print("   res:", res)
print("   点击序列:", [c for c in inp2.calls if c[0] == "hover"][:6])

print()
print("== 用例3：click_confirm 点「是」也是双击，关了就不再点 ==")
state = {"n": 0}


def fake_dialog_score(img, opt):
    state["n"] += 1
    return 0.0 if state["n"] >= 1 else 0.99   # 点第一次后就判定弹窗已关


sc.dialog_score = fake_dialog_score
seq["n"] = 0
inp3 = FakeInp()
n = sc.click_confirm(W(), inp3, CFG, "island", "yes")
print("   click_confirm 返回:", n, "| 点击:", inp3.calls)

print()
print("== 汇总 ==")
ok1 = all(c[3] == 2 and abs(c[4] - 0.12) < 1e-9 for c in inp.calls)
print("  用例1 双击参数:", "PASS" if ok1 else "FAIL")
hovers2 = [c for c in inp2.calls if c[0] == "hover"]
print("  用例2 格子点击次数:", len(hovers2), "→", "PASS" if len(hovers2) == 2 else "FAIL")
print("  用例3 点「是」:", inp3.calls, "→", "PASS" if (n == 1 and inp3.calls and inp3.calls[0][3] == 2) else "FAIL")
