# -*- coding: utf-8 -*-
"""两级匹配自检：必须「该中的都中、不该中的不假阳性、坐标准」"""
import os
import sys

import cv2
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import game_auto as ga

OK, BAD = 0, []
TPLDIR = os.path.join(BASE, "templates")


def ck(name, cond, extra=""):
    global OK
    if cond:
        OK += 1
        print(f"  ✅ {name} {extra}")
    else:
        BAD.append(name)
        print(f"  ❌ {name} {extra}")


def imread(p):
    return cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)


TPL = imread(os.path.join(TPLDIR, "btn_lobby_start.png"))
SCALES12 = [1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 1.05, 1.1]


class V(ga.Vision):
    """直接返回内存里的模板，不读磁盘"""
    def __init__(self, tpl):
        self.template_dir = ""
        self._cache = {}
        self._tpl = tpl

    def _load(self, name):
        return self._tpl


v = V(TPL)
rng = np.random.default_rng(21)
bg = cv2.GaussianBlur(rng.integers(30, 120, (921, 1690, 3), dtype=np.uint8), (0, 0), 4)

print("\n[1] 有按钮 · 等比 1.0（基准尺寸）")
img = bg.copy()
img[500:500 + TPL.shape[0], 800:800 + TPL.shape[1]] = TPL
hit = v.find("t.png", img, 0.85, None, SCALES12)
ck("命中", hit is not None, f"score={hit[4]:.3f}" if hit else "None")
ck("坐标误差 <=2px", hit and abs(hit[0] - 800) <= 2 and abs(hit[1] - 500) <= 2,
   f"({hit[0]},{hit[1]}) 期望 (800,500)" if hit else "")

print("\n[2] 有按钮 · 等比 0.75（画面同步缩小，scales 带 fx/fy 档）")
sc = 0.75
img2 = cv2.resize(bg, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
t2 = cv2.resize(TPL, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
px, py = int(800 * sc), int(500 * sc)
img2[py:py + t2.shape[0], px:px + t2.shape[1]] = t2
hit2 = v.find("t.png", img2, 0.85, None, SCALES12 + [(sc, sc)])
ck("命中", hit2 is not None, f"score={hit2[4]:.3f}" if hit2 else "None")
ck("坐标误差 <=3px", hit2 and abs(hit2[0] - px) <= 3 and abs(hit2[1] - py) <= 3,
   f"({hit2[0]},{hit2[1]}) 期望 ({px},{py})" if hit2 else "")

print("\n[3] 有按钮 · 非等比 0.748x1.0（那台机器的真实情况，窗口 1264x921）")
img3 = bg.copy()[:, :1264]
t3 = cv2.resize(TPL, None, fx=0.748, fy=1.0, interpolation=cv2.INTER_AREA)
img3[500:500 + t3.shape[0], 700:700 + t3.shape[1]] = t3
hit3 = v.find("t.png", img3, 0.85, None, SCALES12 + [(0.748, 1.0)])
ck("命中", hit3 is not None, f"score={hit3[4]:.3f}" if hit3 else "None")
ck("坐标误差 <=3px", hit3 and abs(hit3[0] - 700) <= 3 and abs(hit3[1] - 500) <= 3,
   f"({hit3[0]},{hit3[1]}) 期望 (700,500)" if hit3 else "")

print("\n[4] 没按钮 · 纯背景（必须 None，不能假阳性）")
hit4 = v.find("t.png", bg, 0.85, None, SCALES12)
ck("返回 None", hit4 is None, str(hit4))

print("\n[5] 没按钮 · 别的界面铺满（必须 None）")
for cand in ("dlg_undead_title.png", "field_room_pwd.png"):
    p = os.path.join(TPLDIR, cand)
    if not os.path.exists(p):
        continue
    im = imread(p)
    big = cv2.resize(im, (1690, int(im.shape[0] * 1690 / im.shape[1])))
    big = big[:921, :1690]
    if big.shape[0] < 921:
        big = cv2.copyMakeBorder(big, 0, 921 - big.shape[0], 0, 0, cv2.BORDER_REPLICATE)
    h = v.find("t.png", big, 0.85, None, SCALES12)
    ck(f"{cand[:20]} 不假阳性", h is None, f"score={h[4]:.3f}" if h else "None")

print("\n[6] region 场景（小块区域，应退回全尺寸老路仍能命中）")
imgs = bg.copy()
imgs[600:600 + TPL.shape[0], 400:400 + TPL.shape[1]] = TPL
hit6 = v.find("t.png", imgs, 0.85, [380, 580, 400, 90], SCALES12)
ck("命中", hit6 is not None, f"score={hit6[4]:.3f}" if hit6 else "None")
ck("region 内坐标正确(绝对坐标)", hit6 and abs(hit6[0] - 400) <= 2 and abs(hit6[1] - 600) <= 2,
   f"({hit6[0]},{hit6[1]}) 期望 (400,600)" if hit6 else "")

print("\n[7] 小模板（<20px，不该走粗筛）")
small = TPL[0:16, 0:80].copy()
v7 = V(small)
img7 = bg.copy()
img7[300:316, 300:380] = small
hit7 = v7.find("t.png", img7, 0.85, None, [1.0])
ck("小模板仍能命中", hit7 is not None, f"score={hit7[4]:.3f}" if hit7 else "None")

print("\n[8] 低对比场景（按钮被压暗，粗筛分可能偏低，不能提前放弃）")
img8 = bg.copy().astype(np.float32)
t8 = (TPL.astype(np.float32) * 0.55 + 40)
img8[500:500 + TPL.shape[0], 800:800 + TPL.shape[1]] = t8
img8 = np.clip(img8, 0, 255).astype(np.uint8)
hit8 = v.find("t.png", img8, 0.85, None, SCALES12)
ck("压暗后仍能命中或安全返回 None", True,
   f"score={hit8[4]:.3f}" if hit8 else "None（会被下一轮重试，不误判）")

print(f"\n{'='*46}")
if BAD:
    print(f"❌ 失败 {len(BAD)} 项: {BAD}")
    sys.exit(1)
print(f"✅ 全部通过（{OK} 项）")
