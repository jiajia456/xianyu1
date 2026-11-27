# -*- coding: utf-8 -*-
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
import secret_challenge as sc

CFG_REAL = sc.load_cfg()
ORIG_LOCATE = sc.locate_npc
ORIG_SLEEP = sc.time.sleep

print("== 用例1：候选点队列 ==")
sc.LAST_CANDS = [(1120, 273, "四字光圈备700px"), (1099, 273, "两字光圈备680px")]
sc.LAST_LABEL = (1120, 203, "四字")
q = sc._click_queue((1102, 273, 0.71, 1.0), CFG_REAL)
print("   候选数:", len(q), "前 5:", q[:5])
keys = [(x // 9, y // 9) for x, y, _s in q]
ok1 = (len(q) >= 8 and q[0][:2] == (1102, 273)
       and any("标签+" in s for _x, _y, s in q)
       and any("配置坐标" in s for _x, _y, s in q)
       and len(keys) == len(set(keys)))
print("   ", "PASS" if ok1 else "FAIL")

print("== 用例2：前 3 个候选点都点不开 → 第 4 个开 ==")
class FakeInp:
    def __init__(self):
        self.calls = []
    def click(self, x, y, button="left", hold=0.05, clicks=1, gap=0.05):
        self.calls.append(("click", x, y, clicks))
    def click_hover(self, x, y, hover=0.8, hold=0.12, button="left", clicks=1, gap=0.06):
        self.calls.append(("hover", x, y, clicks))
    def key(self, k):
        pass

class W:
    hwnd = 1

CFG = json.loads(json.dumps(CFG_REAL))
CFG["panel_settle"] = 0.0
CFG["npc_hover"] = 0.0
sc.time.sleep = lambda *a, **k: None
sc.prepare_view = lambda *a, **k: None
sc.ig.grab_checked = lambda w: np.zeros((921, 1690, 3), np.uint8)
sc.locate_npc = lambda img, c=None, verbose=True: (1102, 273, 0.71, 1.0)

inp = FakeInp()
def fake_panel(img, c=None, threshold=None):
    done = len([x for x in inp.calls if x[0] == "hover"])
    ok = done >= 3
    return ok, {"island": (1.0 if ok else 0.47, (1373, 743)),
                "undead": (1.0 if ok else 0.46, (1449, 743)), "threshold": 0.62}
sc.panel_secret_open = fake_panel
x, y, ok = sc.open_panel(W(), inp, CFG, verbose=True)
clicks = [c for c in inp.calls if c[0] == "hover"]
ok2 = ok and len(clicks) == 3 and clicks[0][3] == 1 and all(c[3] == 2 for c in clicks[1:])
print("   结果:", (x, y, ok), "| 点击:", clicks)
print("   ", "PASS" if ok2 else "FAIL")

print("== 用例3：面板一直不开 → 报失败 + 落诊断图，候选数受 max_click_tries 限制 ==")
sc.panel_secret_open = lambda img, c=None, threshold=None: (
    False, {"island": (0.47, (1373, 743)), "undead": (0.46, (1449, 743)), "threshold": 0.62})
CFG["max_click_tries"] = 5
inp2 = FakeInp()
x, y, ok = sc.open_panel(W(), inp2, CFG, verbose=False)
n2 = len([c for c in inp2.calls if c[0] == "hover"])
files = [f for f in os.listdir("logs") if f.startswith("secret_npc_fail_")]
ok3 = (ok is False) and n2 == 5 and len(files) > 0
print("   点了", n2, "下 | 结果", ok, "| 诊断图", sorted(files)[-1:] if files else "无")
print("   ", "PASS" if ok3 else "FAIL")

print("== 用例4：标签完全找不到时，NPC 模型通道要能把 NPC 找出来 ==")
sc.locate_npc = ORIG_LOCATE
sc.time.sleep = ORIG_SLEEP
tpl = cv2.imdecode(np.fromfile("templates/npc_secret.png", dtype=np.uint8), cv2.IMREAD_COLOR)
img = np.full((921, 1690, 3), 55, np.uint8)
cv2.randn(img, 60, 6)
th, tw = tpl.shape[:2]
PX, PY = 1000, 200
img[PY:PY + th, PX:PX + tw] = tpl
loc = ORIG_LOCATE(img, CFG_REAL, verbose=True)
want = (PX + tw // 2, PY + th // 2)
ok4 = loc is not None and abs(loc[0] - want[0]) <= 6 and abs(loc[1] - want[1]) <= 6
print("   期望", want, "实际", loc[:2] if loc else None)
print("   ", "PASS" if ok4 else "FAIL")

print("== 汇总 ==", "全部通过" if (ok1 and ok2 and ok3 and ok4) else "有失败")
sys.exit(0 if (ok1 and ok2 and ok3 and ok4) else 1)
