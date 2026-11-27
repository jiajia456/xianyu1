"""红包广告弹窗识别自检（离线，不需要开游戏）

用户 2026-11-17 20:58：房间里随机弹「216U币 红包」广告，**它是模态的** ——
弹着的时候点「选难度」「开始游戏」全都没反应（视觉上并不遮住按钮）。

这里验证三件事：
  ① 用现场截图能命中「红包广告」，且推算的 X 位置与真实 X 一致（误差 ≤ 3px）
  ② 窗口被缩放（0.8 / 0.9 / 1.1 / 1.2 倍）时仍能命中，X 位置跟着等比换算
  ③ 不误报：首充礼包模板不会在红包界面上命中，红包模板也不会在其它截图上命中

样本图缺了就跳过该项（不硬失败），方便换机器跑。
"""
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ad_guard   # noqa: E402

SAMPLE = (r"C:\Users\Administrator\.workbuddy\clipboard-images"
          r"\clipboard-2026-11-17T12-58-47-421Z-169bd5cb.png")
X_REAL = (648, 180)          # 现场截图里白色 X 的中心（人工量的，见 README 六十二节）
REDPACKET = "红包广告"
FIRSTCHARGE = "首充礼包"


def rd(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def tpl_of(name):
    for t in ad_guard._tpls():
        if t["name"] == name:
            return t
    raise SystemExit(f"模板里没有「{name}」")


def hit(tname, img):
    """和 ad_guard.find() 同一条判定链路：相似度阈值 + 区域长相校验"""
    t = tpl_of(tname)
    r = ad_guard._match_in(img, t["img"], t["score"])
    if not r:
        return None
    score, sc, loc, size = r
    if not ad_guard._patch_ok(img, loc, size, t):
        return None
    xy = (int(loc[0] + size[0] * t["dx"]), int(loc[1] + size[1] * t["dy"]))
    return dict(score=score, scale=sc, loc=loc, size=size, xy=xy)


def case(title, ok, detail=""):
    print(f"{'✅' if ok else '❌'} {title}" + (f"  {detail}" if detail else ""))
    return ok


def main():
    ok_all = True
    if not os.path.exists(SAMPLE):
        print(f"⚠ 样本图不存在，跳过：{SAMPLE}")
        return 0
    src = rd(SAMPLE)

    # ① 现场图命中 + 坐标准
    h = hit(REDPACKET, src)
    ok_all &= case("现场截图命中红包广告", h is not None and h["score"] >= 0.90,
                   "" if not h else f"score={h['score']:.3f}")
    if h:
        dx = abs(h["xy"][0] - X_REAL[0])
        dy = abs(h["xy"][1] - X_REAL[1])
        ok_all &= case("推算的 X 与真实 X 一致（≤3px）", dx <= 3 and dy <= 3,
                       f"算得 {h['xy']} / 实际 {X_REAL} 差 ({dx},{dy})")

    # ② 跨缩放
    for f in (0.8, 0.9, 1.1, 1.2):
        img = cv2.resize(src, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        h2 = hit(REDPACKET, img)
        want = (round(X_REAL[0] * f), round(X_REAL[1] * f))
        good = h2 is not None and h2["score"] >= 0.85 \
            and abs(h2["xy"][0] - want[0]) <= 4 and abs(h2["xy"][1] - want[1]) <= 4
        ok_all &= case(f"窗口 {f:.1f} 倍仍命中且坐标等比", good,
                       "" if not h2 else f"score={h2['score']:.3f} 算得 {h2['xy']} 预期 {want}")

    # ③ 反例一：首充模板不该认成红包界面
    h3 = hit(FIRSTCHARGE, src)
    ok_all &= case("首充模板不在红包界面上误命中", h3 is None,
                   "" if not h3 else f"score={h3['score']:.3f}（阈值 {tpl_of(FIRSTCHARGE)['score']}）")

    # 反例二：红包模板不该在大厅/其它平台截图上命中
    others = [p for p in ("logs/diff_rows/lobby_after_full.png", "logs/plat_1080.png")
              if os.path.exists(os.path.join(HERE, p))]
    for p in others:
        img = rd(os.path.join(HERE, p))
        if img is None:
            continue
        h4 = hit(REDPACKET, img)
        ok_all &= case(f"红包模板不在 {os.path.basename(p)} 上误命中", h4 is None,
                       "" if not h4 else f"score={h4['score']:.3f}")

    print()
    print("全部通过 ✅" if ok_all else "有失败项 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
