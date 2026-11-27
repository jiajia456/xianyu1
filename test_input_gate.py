# -*- coding: utf-8 -*-
"""输入框「真的写进去了吗」判据测试（2026-11-27 17:35 用户现场）。

事故：W10「创建房间」弹窗里密码框明明是空的，旧判据只看区域方差（8.46→13.56，
光标闪烁/重绘就能造成），判了「输入生效」→ 点「创建」→ 开成无密码房。

改法：方差变化 + **像素变化占比** 双判据。字符/圆点会造成 1% 以上的像素变化，
光标闪一下通常 <0.4%。本测试用合成图验证两个方向都不会判错。

跑法：python test_input_gate.py
"""
from __future__ import annotations

import io
import json

import numpy as np
import cv2

import game_auto as ga

OK = 0
FAIL = 0
RATIO_MIN = 0.006          # 与 game_auto.type_into 的默认 min_ratio 保持一致


def ck(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


def blank():
    """空密码框：深色底，纯色（方差≈0，但真实场景有边框/水印，见下面的 noisy）"""
    return np.full((26, 395, 3), 28, np.uint8)


def noisy():
    """空密码框的真实样子：带边框/水印，方差有 8~9，所以只看方差会误判"""
    p = blank()
    cv2.rectangle(p, (0, 0), (394, 25), (55, 55, 55), 1)
    for x in range(0, 395, 7):
        p[12:14, x:x + 2] = (34, 34, 34)
    return p


def typed(n=2):
    """输入 'Aa' 后：框里出现 n 个白色圆点（遮码）"""
    p = noisy()
    for i in range(n):
        cx = 24 + i * 16
        cv2.circle(p, (cx, 13), 5, (245, 245, 245), -1)
    return p


def cursor_blink():
    """光标闪一下：只有 2x14 像素变化"""
    p = noisy()
    p[6:20, 40:42] = (210, 210, 210)
    return p


def main():
    print("=" * 60)
    print("输入生效判据（方差 + 像素变化占比）")
    print("=" * 60)
    D = ga.Bot._patch_diff_ratio

    print("\n[1] 真的输入了 → 必须判成功")
    r = D(noisy(), typed(2))
    ck("'Aa' 两个圆点变化占比 >= %.1f%%" % (RATIO_MIN * 100),
       r >= RATIO_MIN, "ratio=%.3f" % r)
    r1 = D(noisy(), typed(1))
    ck("单个字符也够（>=%.1f%%）" % (RATIO_MIN * 100), r1 >= RATIO_MIN, "ratio=%.3f" % r1)

    print("\n[2] 只有光标闪 → 必须判失败（这就是 17:35 那次误判）")
    r2 = D(noisy(), cursor_blink())
    ck("光标闪烁占比 < %.1f%%" % (RATIO_MIN * 100), r2 < RATIO_MIN, "ratio=%.3f" % r2)

    print("\n[3] 完全没变化 → 判失败")
    ck("同一张图 ratio=0", D(noisy(), noisy()) == 0.0)

    print("\n[4] 异常输入不崩")
    ck("None 传进去返回 0", D(None, noisy()) == 0.0 and D(noisy(), None) == 0.0)
    ck("尺寸不一致返回 0", D(noisy(), np.zeros((10, 10, 3), np.uint8)) == 0.0)

    print("\n[5] 旧判据的反例（方差会骗人）")
    b, a = noisy(), cursor_blink()
    ck("光标闪烁：方差差 > 0.35（旧判据会误判成功）",
       abs(float(a.std()) - float(b.std())) > 0.35,
       "%.2f→%.2f" % (b.std(), a.std()))
    ck("但新判据拦得住", D(b, a) < RATIO_MIN)

    print("\n[6] 点「创建」前的最后闸：密码框内容像素")
    I = ga.Bot._patch_ink
    ink_blank = I(noisy())
    ink_typed = I(typed(2))
    ck("空框内容像素 < 40（占位灰字不算）", ink_blank < 40, "ink=%d" % ink_blank)
    ck("输入 'Aa' 后内容像素 >= 40", ink_typed >= 40, "ink=%d" % ink_typed)
    ck("只有一个圆点也够 >= 20", I(typed(1)) >= 20, "ink=%d" % I(typed(1)))

    print("\n[7] 源码与配置护栏")
    src = open(ga.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    ck("type_into 用了 _patch_diff_ratio", "_patch_diff_ratio(before_patch, after_patch)" in src)
    ck("双判据：方差 且 占比", "abs(after - before) > min_delta and _ratio >= min_ratio" in src)
    ck("失败仍抛错（required 时不会继续点创建）",
       "raise RuntimeError(f\"输入失败，{retries} 次都没写进去" in src)
    ck("新增 ensure_pwd 步骤（点创建前复核 + 空则补输）",
       '"ensure_pwd"' in src and 'if kind in ("assert_ink", "check_ink", "ensure_pwd"):' in src)
    ck("空框会继续输入（补输循环）", "→ 补输第 {attempt}/{tries} 次" in src)
    ck("补输仍空才抛错", "密码框始终是空的" in src)
    cfg = json.load(io.open("configs/wuxia_dialog.json", encoding="utf-8"))
    kinds = [list(s)[0] for s in cfg["tasks"]["fill_and_submit"]["steps"]]
    ck("配置里 ensure_pwd 在 click_image 之前",
       "ensure_pwd" in kinds and kinds.index("ensure_pwd") < kinds.index("click_image"),
       str(kinds))
    _ep = [x["ensure_pwd"] for x in cfg["tasks"]["fill_and_submit"]["steps"] if "ensure_pwd" in x][0]
    ck("ensure_pwd 带 text/point/tries（能自己补输）",
       _ep.get("text") and _ep.get("point") and int(_ep.get("tries", 0)) >= 1, str(_ep))
    ck("fill_and_submit 的 type_into 仍是 required=true",
       any("type_into" in s and s["type_into"].get("required") for s in
           cfg["tasks"]["fill_and_submit"]["steps"]))

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
