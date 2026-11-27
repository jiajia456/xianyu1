# -*- coding: utf-8 -*-
"""自检：③ 点完「创建」后立刻把房间窗抓到前台（用户 2026-11-19 00:11 定的改法）

原来 ③→④ 之间固定 `settle=2500ms` 死等，日志里就是那句
「等 2.5s 让下一个窗口出来」——用户觉得是多余的视觉跳转。
现在改成（2026-11-27）：③ 点完「创建」先固定静默 6s（ROOM_FRONT_DELAY，
等平台销毁弹窗、重建房间窗），再探一次房间窗（新窗口法优先、模板法兜底），
探到就 activate 到前台，探不到才退回 settle 兜底。

用法：python test_pipeline_room_front.py
"""
from __future__ import annotations

import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAIL = []


def check(name, cond, detail=""):
    print(("  \u2705 " if cond else "  \u274c ") + name + (f"  ({detail})" if detail else ""))
    if not cond:
        FAIL.append(name)


def main():
    import game_auto as ga
    import room_finder as rf

    real_find, real_act = rf.find_room, rf.activate
    acts = []

    print("① 房间窗已经出来 → 立刻抓到前台，不再等")
    try:
        room = {"hwnd": 777, "client": (0, 0, 1224, 904)}
        rf.find_room = lambda verbose=False: room
        rf.activate = lambda h, settle=0.5: acts.append(h)
        t0 = time.time()
        r, waited = ga.await_room_front()
        cost = time.time() - t0
        check("返回房间信息", r is room, f"r={r}")
        check("已调用 activate 抓前台", acts == [777], f"acts={acts}")
        check("几乎不花时间（<0.5s）", cost < 0.5, f"{cost:.2f}s")
    finally:
        rf.find_room, rf.activate = real_find, real_act
        acts.clear()

    print("② 房间窗一直不出来 → 立即返回 None（交给 settle 兜底）")
    try:
        rf.find_room = lambda verbose=False: None
        rf.activate = lambda h, settle=0.5: acts.append(h)
        t0 = time.time()
        r, waited = ga.await_room_front()
        cost = time.time() - t0
        check("返回 None", r is None)
        check("不空等（一次探测就返回）", waited < 1.0, f"waited={waited:.2f}")
        check("超时期间不点任何东西", acts == [], f"acts={acts}")
        check("耗时很短，没有乱拖", cost < 1.5, f"{cost:.2f}s")
    finally:
        rf.find_room, rf.activate = real_find, real_act
        acts.clear()

    print("③ 异常不外抛（房间探测/激活炸了也不能毁掉整条链路）")
    try:
        def _boom(verbose=False):
            raise RuntimeError("模拟探测炸了")
        rf.find_room = _boom
        r, _ = ga.await_room_front()
        check("find_room 抛异常 → None 且不外抛", r is None)

        rf.find_room = lambda verbose=False: {"hwnd": 888, "client": (0, 0, 1224, 904)}

        def _boom2(h, settle=0.5):
            raise RuntimeError("模拟 activate 炸了")
        rf.activate = _boom2
        r2, _ = ga.await_room_front()
        check("activate 抛异常 → 仍然返回房间信息", r2 and r2["hwnd"] == 888)
    finally:
        rf.find_room, rf.activate = real_find, real_act

    print("④ 源码护栏（别把改法改回去）")
    src = io.open(os.path.join(HERE, "game_auto.py"), encoding="utf-8").read()
    check("步尾用「下一步是 activate_room」触发抓前台", 'nxt.get("activate_room")' in src)
    check("抓到房间窗就把 settle 归零", "settle = 0.0" in src)

    print()
    if FAIL:
        print(f"\u274c 失败 {len(FAIL)} 项：" + "、".join(FAIL))
        return 1
    print("\u2705 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
