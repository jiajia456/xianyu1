# -*- coding: utf-8 -*-
"""v1.8 离线自检：非等比缩放匹配 / 窗口重绑 / cap_ref 计算"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import cv2                                                    # noqa: E402
import numpy as np                                            # noqa: E402
import game_auto as ga                                        # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + extra) if extra else ""))
    ok = ok and bool(cond)


print("① find() 支持非等比缩放 (0.748, 1.0)")
# 造模板：100x40，带明显纹理
tpl = np.zeros((40, 100, 3), np.uint8)
tpl[:, :50] = (40, 40, 200)
cv2.rectangle(tpl, (10, 8), (88, 32), (250, 250, 250), 2)
cv2.putText(tpl, "GO", (26, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
# 造画面：把模板按 (0.748, 1.0) 缩放后贴在 (300, 200)
tpl_x = cv2.resize(tpl, None, fx=0.748, fy=1.0, interpolation=cv2.INTER_AREA)
screen = np.full((921, 1264, 3), 30, np.uint8)
screen[200:200 + tpl_x.shape[0], 300:300 + tpl_x.shape[1]] = tpl_x

vis = ga.Vision(os.path.join(ROOT, "templates"))


def find_with(name, img, scales):
    """直接用 Vision.find 但换掉模板加载"""
    orig = vis._load
    vis._load = lambda n: tpl if n == "__t__" else orig(n)
    try:
        return vis.find("__t__", img, 0.85, None, scales)
    finally:
        vis._load = orig


hit_uni = find_with("uni", screen, [1.0])
hit_xy = find_with("xy", screen, [1.0, (0.748, 1.0)])
check("只试等比 → 找不到（复现那台电脑的失败）", hit_uni is None)
check("加一档 (0.748,1.0) → 命中", hit_xy is not None,
      "" if not hit_xy else f"@({hit_xy[0]},{hit_xy[1]}) score={hit_xy[4]:.3f}")
if hit_xy:
    check("命中位置正确（左上角约 300,200）",
          abs(hit_xy[0] - 300) <= 3 and abs(hit_xy[1] - 200) <= 3,
          f"偏差 ({hit_xy[0] - 300},{hit_xy[1] - 200})")

print("② Bot._scales_with_ref：1264x921 窗口自动补 (0.748, 1.0)")


class _W:
    def client_rect(self):
        return (0, 0, 1264, 921)


# 2026-11-18 18:56：_scales_with_ref 现在委托给 _scales_for，必须用真的 Bot 实例
b = ga.Bot.__new__(ga.Bot)
b._cap_ref = [1690, 921]
b.win = _W()
sc = ga.Bot._scales_with_ref(b, {"scales": [1.0, 0.9]})
check("补上了非等比档", (0.748, 1.0) in sc, str(sc))
b.win = type("W2", (), {"client_rect": lambda self: (0, 0, 1690, 921)})()
sc2 = ga.Bot._scales_with_ref(b, {"scales": [1.0]})
check("基准窗口不额外加档（本机零影响）", sc2 == [1.0], str(sc2))

# ③④ 原来的「句柄重绑」用例已随功能一起删除（2026-11-18 18:49 用户要求：
#     不再按同进程/尺寸猜窗口，改成「谁画出大厅画面就绑谁」）。
#     新逻辑的用例在 test_content_bind.py 里。
print("③ 句柄重绑已废弃 → 见 test_content_bind.py（按画面内容绑定）")

print()
print("结论：" + ("全部通过 ✅" if ok else "有失败 ❌"))
sys.exit(0 if ok else 1)
