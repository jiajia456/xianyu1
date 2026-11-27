# -*- coding: utf-8 -*-
"""自检：签到脚本遇到「首充礼包」这类广告弹窗时的处理（2026-11-19 00:07 线上踩坑）

现象：退出游戏后平台弹了「首充礼包 连锁三天」独立小窗（924x562，标题也是
「KK官方对战平台」）→ 被 find_panel 的 fallback 当成「签到弹窗已开着」→
里面既没「立即签到」也没「今日已签到」→ 直接退出码 5，整段签到领券白跑。

本测试只验纯逻辑（不碰真窗口）：
  1) clear_ads 认得 ad_guard 的四种返回：closed / none / failed / 抛异常
  2) _panel_kind 在没窗/有窗时的行为
  3) 源码里必须留着「先清广告」和「不认识的弹窗要收掉」两处护栏

用法：python test_sign_ad.py
"""
from __future__ import annotations

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAIL = []


def check(name, cond, detail=""):
    print(("  \u2705 " if cond else "  \u274c ") + name + (f"  ({detail})" if detail else ""))
    if not cond:
        FAIL.append(name)


def main():
    import ad_guard
    import daily_sign as ds

    real_close = ad_guard.close
    real_grab = ds.pg.grab_hwnd
    real_state = ds.panel_state

    print("① clear_ads：ad_guard.close 各种返回值")
    try:
        for ret, want in (("closed", True), ("none", False), ("failed", False)):
            ad_guard.close = lambda verbose=True, _r=ret: _r
            got = ds.clear_ads(verbose=False)
            check(f"close()='{ret}' → {want}", got is want, f"got={got}")

        def _boom(verbose=True):
            raise RuntimeError("模拟 ad_guard 内部炸了")
        ad_guard.close = _boom
        check("close() 抛异常 → False 且不外抛", ds.clear_ads(verbose=False) is False)

        # ad_guard 整个模块都 import 不进来时也不能影响签到主流程
        sys.modules["ad_guard"] = None
        try:
            check("ad_guard 不可用 → False 且不外抛", ds.clear_ads(verbose=False) is False)
        finally:
            sys.modules["ad_guard"] = ad_guard
    finally:
        ad_guard.close = real_close

    print("② _panel_kind：判「开着的那个窗」是不是签到弹窗")
    try:
        check("panel=None → None", ds._panel_kind(None) is None)

        ds.pg.grab_hwnd = lambda h: None
        check("抓不到图 → None", ds._panel_kind(123) is None)

        ds.pg.grab_hwnd = lambda h: "fake-frame"
        ds.panel_state = lambda img: ("todo", None)
        check("有「立即签到」→ todo", ds._panel_kind(123) == "todo")

        ds.panel_state = lambda img: (None, None)
        check("广告窗（两个都不认识）→ None", ds._panel_kind(123) is None)
    finally:
        ds.pg.grab_hwnd = real_grab
        ds.panel_state = real_state

    print("③ 源码护栏（防止有人把这两处逻辑删掉）")
    src = io.open(os.path.join(HERE, "daily_sign.py"), encoding="utf-8").read()
    check("签到前先清广告", "clear_ads(verbose=True)" in src)
    check("不认识的弹窗要收掉（不再留给 ② 领券挡路）",
          "\u6536\u6389\u8fd9\u4e2a\u4e0d\u8ba4\u8bc6\u7684\u5f39\u7a97" in src)
    claim = io.open(os.path.join(HERE, "daily_claim.py"), encoding="utf-8").read()
    check("领券前也清广告", "ad_guard.close(verbose=True)" in claim)

    print()
    if FAIL:
        print(f"\u274c 失败 {len(FAIL)} 项：" + "、".join(FAIL))
        return 1
    print("\u2705 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
