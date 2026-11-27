# -*- coding: utf-8 -*-
"""不死秘境「走向诅咒神庙」参数护栏（2026-11-27 18:17 现场）。

事故：走 90 秒 / 111 次 ↑，「诅咒神庙」文字最高只到 0.765 < 0.80 → 判定没认到
→ 放弃神庙 → 直接等结算 → 玩家看到「选完不死不进下一阶段」。

改法：阈值 0.80 → 0.70（配置 undead_temple.text_th），要求连续 frames 帧达标，
一轮没到就再走一轮（rounds），全部可配置。

跑法：python test_temple_params.py
"""
from __future__ import annotations

import inspect
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import secret_challenge as sc

OK = 0
FAIL = 0


def ck(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


def main():
    print("=" * 60)
    print("不死神庙走位参数")
    print("=" * 60)
    sig = inspect.signature(sc.walk_until_gate)
    p = sig.parameters
    print("\n[1] 函数签名")
    ck("默认 text_th 已降到 0.70", abs((p["text_th"].default or 0) - 0.70) < 1e-6,
       str(p["text_th"].default))
    ck("支持 frames（连续帧确认）", "frames" in p, str(list(p)))
    ck("支持 rounds（多轮走）", "rounds" in p, str(list(p)))
    ck("frames 默认 >=2", int(p["frames"].default) >= 2, str(p["frames"].default))
    ck("rounds 默认 >=2", int(p["rounds"].default) >= 2, str(p["rounds"].default))

    print("\n[2] 配置文件")
    cfg = json.load(io.open(os.path.join(HERE, "configs", "secret_challenge.json"),
                            encoding="utf-8"))
    t = cfg.get("undead_temple") or {}
    ck("有 undead_temple 配置块", bool(t))
    ck("text_th <= 0.72（能过 0.765 的现场最高分）",
       float(t.get("text_th", 1)) <= 0.72, str(t.get("text_th")))
    ck("frames >= 2", int(t.get("frames", 0)) >= 2, str(t.get("frames")))
    ck("rounds >= 2", int(t.get("rounds", 0)) >= 2, str(t.get("rounds")))
    ck("timeout > 0", float(t.get("timeout", 0)) > 0, str(t.get("timeout")))

    print("\n[3] 调用处读配置（不是写死）")
    src = io.open(os.path.join(HERE, "secret_challenge.py"), encoding="utf-8").read()
    ck("调用 walk_until_gate 时传 text_th",
       "text_th=float(_tw.get(\"text_th\"" in src)
    ck("调用时传 rounds/frames",
       "rounds=int(_tw.get(\"rounds\"" in src and "frames=int(_tw.get(\"frames\"" in src)
    ck("多轮循环存在", "第 {rnd}/{rounds} 轮" in src)
    ck("连续帧判定存在", "hit >= max(1, frames)" in src)

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
