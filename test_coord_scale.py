# -*- coding: utf-8 -*-
"""小窗口下的「截图归一化 + 点击坐标换算」离线桩测试。

背景（用户 2026-11-17 18:32 / 18:38）：换到另一台电脑，游戏窗口比调参那台小很多
（约 0.63 倍，客户区 ~1065x580）。所有坐标常量与模板都是按 1690x921 调的，于是
  · 选难度：一行都识别不到 → 卡在难度界面
  · 绑定窗口：min_size=600 直接把 580 高的窗口过滤掉 → 「找不到窗口」
本测试验证修复后的两件事：
  ① in_game.norm() 能把小图还原到 1690x921，局内判据（暗色/红色像素）结论不变；
  ② Input(ref_size=...) 会把归一化坐标按实际客户区放大回去，落点还是原来那个像素。

运行： python test_coord_scale.py
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import game_auto as ga          # noqa: E402
import in_game as ig           # noqa: E402
import wave_auto as wv         # noqa: E402

REF = (1690, 921)


def imread_u(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


class FakeWin:
    def __init__(self, cw, ch):
        self.cw, self.ch = cw, ch

    def client_rect(self):
        return (0, 0, self.cw, self.ch)

    def ensure_foreground(self, wait: float = 0.2):
        return True

    def alive(self):
        return True


def main():
    ok = True

    # ---------- ① norm 之后判据是否一致 ----------
    try:
        src = imread_u(os.path.join(HERE, "logs", "war3_now_1710.png"))
    except Exception:
        src = None
    if src is None:
        print("⚠ 缺少局内样本 logs/war3_now_1710.png，跳过判据对比")
    else:
        print(f"局内样本 {src.shape[1]}x{src.shape[0]}")
        base = ig.norm(src)
        assert base.shape[:2] == (REF[1], REF[0]), base.shape
        for f in (0.63, 0.7, 0.8):
            small = cv2.resize(src, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
            back = ig.norm(small)
            # 局内「面板是否开着」用的是深色占比（REG_SKILL 区域）
            def dark_ratio(img, reg=ig.REG_SKILL):
                x0, y0, x1, y1 = reg
                roi = img[y0:y1, x0:x1]
                g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                return float((g < 60).mean())
            r0, r1 = dark_ratio(base), dark_ratio(back)
            ok1 = abs(r0 - r1) < 0.10
            # 滚波模式判据（红色像素）
            n0, n1 = wv.red_pixels(base), wv.red_pixels(back)
            ok2 = (n0 >= wv.RED_MIN) == (n1 >= wv.RED_MIN)
            print(f"{'✅' if ok1 and ok2 else '❌'} 缩放 {f:<5} → norm {back.shape[1]}x{back.shape[0]}"
                  f" | 深色占比 {r0:.3f} vs {r1:.3f}"
                  f" | 红像素 {n0} vs {n1}")
            ok = ok and ok1 and ok2

    # ---------- ② 点击坐标换算 ----------
    shots = []
    real_move = ga.pydirectinput.moveTo
    ga.pydirectinput.moveTo = lambda x, y: shots.append((int(x), int(y)))
    try:
        for cw, ch in ((1690, 921), (1352, 737), (1065, 580), (930, 507)):
            shots.clear()
            inp = ga.Input(FakeWin(cw, ch), humanize=False, click_jitter=0,
                           dry_run=False, ref_size=REF)
            for (x, y) in ((840, 338), (1151, 750), (293, 632)):
                inp.move(x, y)
            exp = [(int(round(x * cw / REF[0])), int(round(y * ch / REF[1])))
                   for (x, y) in ((840, 338), (1151, 750), (293, 632))]
            good = shots == exp
            ok = ok and good
            print(f"{'✅' if good else '❌'} 客户区 {cw}x{ch} 点击换算 → {shots}（期望 {exp}）")
        # 不给 ref_size 时必须保持老行为（坐标原样）
        shots.clear()
        inp = ga.Input(FakeWin(1065, 580), humanize=False, click_jitter=0, dry_run=False)
        inp.move(840, 338)
        good = shots == [(840, 338)]
        ok = ok and good
        print(f"{'✅' if good else '❌'} 不传 ref_size → 原样 ({shots})")
    finally:
        ga.pydirectinput.moveTo = real_move

    print()
    print("全部通过 ✅" if ok else "有失败项 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
