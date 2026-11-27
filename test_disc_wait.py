# -*- coding: utf-8 -*-
"""等大厅期间「断线重连」弹窗的自检（用户 2026-11-27 16:20 定的新规则）

规则：
  · 等大厅按钮最长 15 分钟，这期间平台可能弹「断线重连（您先前的游戏还在进行中）」，
    小弹窗把按钮挡住，永远判不到 → 等待循环里**每 6 秒**（disc_interval_s）查一次，
    3 秒后首查。
  · 命中 → **先按 ESC**；ESC 关不掉才点「取消」（绝不点「断线重连(30)」）。
  · 紧随其后的「断线重连失败。」→ 同样 **先 ESC**；不行点「确定」。
  · 弹窗收掉后看房间窗：
      · 「开始游戏」亮着 → 抛 GameGone，交回 pipeline 回 ④ 重新点开始游戏（不重启平台）；
      · 没亮 / 判不了 → 不重启、不重复点，继续等大厅。
  · 全程不退出房间、不关闭窗口、不杀进程、不重启平台。

本文件验证：
  ① 弹窗一开始就在 + 房间可开局 → 3 秒那次收掉并抛 GameGone；
  ② 弹窗中途才出现 → 前几次不白点，命中那次收掉并抛 GameGone；
  ③ 弹窗一直不开 → 一下都不点，按钮出来正常返回；
  ④ spec["disc_check"]=False → 一次都不查；
  ⑤ 只弹「断线重连失败。」+ 房间可开局 → 点「确定」并抛 GameGone；
  ⑥ 弹窗收掉但房间「开始游戏」没亮 → 不抛 GameGone，继续等大厅直到按钮出现；
  ⑦ 弹窗 ESC/取消都关不掉 → 不重启平台、不抛异常，继续等；
  ⑧ 默认巡检间隔 6 秒（代码 + 配置）。
"""
import sys

import game_auto as ga
import disc_guard as dg

FAIL = []


def check(name, cond, extra=""):
    print("   ", "PASS" if cond else "FAIL", name, extra)
    if not cond:
        FAIL.append(name)


class FakeTime:
    """可控时钟：_sleep 推进，避免真实等待。"""

    def __init__(self, t=1000.0):
        self.t = float(t)

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)

    def strftime(self, fmt="%H:%M:%S"):
        return "16:20:00"


class FakeCap:
    def __init__(self, img):
        self._img = img

    def grab(self, win):
        return self._img


class FakeWin:
    hwnd = 1

    def client_rect(self):
        return (0, 0, 1690, 921)


class FakeVis:
    """前 miss_n 次不命中，之后一直命中（模拟「按钮后来才画出来」）。"""

    def __init__(self, miss_n):
        self.miss_n = miss_n
        self.calls = 0

    def find_best(self, tpl, img, th, region, scales):
        self.calls += 1
        if self.calls <= self.miss_n:
            return None, None
        return "btn_lobby_start.png", (860, 600, 180, 46, 0.93)


def make_bot(ft, vis, room_state="ready"):
    import numpy as np
    bot = object.__new__(ga.Bot)          # 不走 __init__，测的就是等待循环本身
    bot.win = FakeWin()
    bot.cap = FakeCap(np.zeros((921, 1690, 3), "uint8"))
    bot.vis = vis
    bot._sleep = lambda ms: ft.sleep(float(ms) / 1000.0)
    bot._checkpoint = lambda: None
    bot._scales_with_ref = lambda spec: [1.0]
    bot._bind_by_content = lambda spec: False
    bot._dump_miss = lambda t: None
    bot._dg = dg
    bot.cfg = {}
    bot._room_state = lambda: room_state
    return bot


HIT = dict(hwnd=4243, w=480, h=300, title="KK官方对战平台",
           score=0.963, scale=1.0, tl=(56, 110), origin=(600, 300), area=480 * 300)
SPEC = {"template": "btn_lobby_start.png", "timeout": 900000,
        "interval": 200, "threshold": 0.85}


def run(bot, spec=None, timeout=900000):
    """跑一次等待循环，返回 (结果, 异常)。"""
    err = None
    res = None
    try:
        res = bot._wait_image_loop(dict(SPEC, timeout=timeout, **(spec or {})))
    except ga.GameGone as e:
        err = e
    except Exception as e:
        err = e
    return res, err


def install_popup(state, body=None, fail=None, close_on_dismiss=True,
                  log=None, appear_at=None, ft=None):
    """装一套「会真的被关掉」的弹窗替身。

    state = {"body": bool, "fail": bool}；被关掉后对应的 find 返回 None，
    这样才符合「ESC/取消 之后弹窗消失」的真实行为。
    """
    state.setdefault("body", bool(body))
    state.setdefault("fail", bool(fail))
    log = log if log is not None else []
    state["log"] = log

    def _vis(key):
        if appear_at is not None and ft is not None and ft.t < appear_at:
            return None
        return HIT if state[key] else None

    def _dismiss(verbose=True, dry=False, tries=3):
        log.append("dismiss")
        if close_on_dismiss and state["body"]:
            state["body"] = False
        return "esc" if close_on_dismiss else "fail"

    def _dismiss_fail(verbose=True, dry=False, tries=3):
        log.append("dismiss_fail")
        if close_on_dismiss and state["fail"]:
            state["fail"] = False
        return "esc" if close_on_dismiss else "fail"

    dg.find = lambda verbose=False, popup_only=True: _vis("body")
    dg.find_fail = lambda verbose=False, popup_only=True: _vis("fail")
    dg.dismiss = _dismiss
    dg.dismiss_fail = _dismiss_fail
    dg._log_event = lambda *a, **k: {}
    return state


def main():
    real_time = ga.time
    # 本文件只测「弹窗怎么点、什么时候点、点完做什么」；进程看门狗由 test_game_gone 负责。
    real_alive = ga.war3_alive
    ga.war3_alive = lambda: True
    left_calls, restart_calls = [], []
    real_leave = None
    real_restart = ga._restart_platform
    try:
        import room_finder
        real_leave = room_finder.leave_room
        room_finder.leave_room = lambda *a, **k: (left_calls.append(1), True)[1]
    except Exception:
        pass
    ga._restart_platform = lambda *a, **k: (restart_calls.append(1), {"status": "ok"})[1]

    # ---------------------------------------------------------------- 用例1
    print("== 用例1：弹窗一开始就在 + 房间可开局 → 收掉并抛 GameGone ==")
    st1 = install_popup({}, body=True, fail=False)
    ft = FakeTime()
    ga.time = ft
    res, err = run(make_bot(ft, FakeVis(miss_n=100000), room_state="ready"))
    cost = ft.t - 1000.0
    ga.time = real_time
    check("抛的是 GameGone", isinstance(err, ga.GameGone), repr(err))
    check("先 ESC/取消（dismiss 只调 1 次）", st1["log"].count("dismiss") == 1, str(st1["log"]))
    check("紧跟查了「断线重连失败」", st1["log"].count("dismiss_fail") == 1, str(st1["log"]))
    check("3 秒那次就收手，没等到按钮", 2 <= cost <= 10, f"cost={cost:.0f}s")
    check("没重启平台", not restart_calls, f"重启 {len(restart_calls)} 次")
    check("全程不退出房间", not left_calls, f"leave_room {len(left_calls)} 次")

    # ---------------------------------------------------------------- 用例2
    print("== 用例2：弹窗中途（第 40 秒）才出现 → 前几次不白点，命中那次收掉 ==")
    ft2 = FakeTime()
    st2 = install_popup({}, body=True, fail=False, appear_at=1040, ft=ft2)
    ga.time = ft2
    res2, err2 = run(make_bot(ft2, FakeVis(miss_n=100000), room_state="ready"))
    ga.time = real_time
    check("也抛 GameGone", isinstance(err2, ga.GameGone), repr(err2))
    check("只点了 1 次", st2["log"].count("dismiss") == 1, str(st2["log"]))
    check("这次点击发生在第 40 秒之后（之前检查都没弹窗 → 没点）",
          ft2.t - 1000.0 >= 40, f"收手在第 {ft2.t - 1000.0:.0f} 秒")

    # ---------------------------------------------------------------- 用例3
    print("== 用例3：弹窗一直不开 → 一下都不点，按钮出来正常返回 ==")
    st3 = install_popup({"body": False, "fail": False})
    ft3 = FakeTime()
    ga.time = ft3
    res3, err3 = run(make_bot(ft3, FakeVis(miss_n=30)), timeout=20000)
    ga.time = real_time
    check("没有误点", not st3["log"], str(st3["log"]))
    check("正常等到按钮", err3 is None and res3 is not None, repr(err3))

    # ---------------------------------------------------------------- 用例4
    print("== 用例4：disc_check=False → 一次都不查 ==")
    n_find = {"v": 0}

    def counting_find(verbose=False, popup_only=True):
        n_find["v"] += 1
        return HIT

    dg.find = counting_find
    dg.find_fail = lambda verbose=False, popup_only=True: None
    log4 = []
    dg.dismiss = lambda verbose=True, dry=False, tries=3: (log4.append("dismiss"), "clicked")[1]
    ft4 = FakeTime()
    ga.time = ft4
    res4, err4 = run(make_bot(ft4, FakeVis(miss_n=20)),
                     {"disc_check": False}, timeout=20000)
    ga.time = real_time
    check("完全没查", n_find["v"] == 0 and not log4,
          f"find {n_find['v']} 次 / 点 {len(log4)} 次")
    check("照原样等到按钮", err4 is None and res4 is not None, repr(err4))

    # ---------------------------------------------------------------- 用例5
    print("== 用例5：只弹「断线重连失败。」+ 房间可开局 → 点「确定」并抛 GameGone ==")
    st5 = install_popup({}, body=False, fail=True)
    ft5 = FakeTime()
    ga.time = ft5
    res5, err5 = run(make_bot(ft5, FakeVis(miss_n=100000), room_state="ready"))
    ga.time = real_time
    check("点了「确定」", st5["log"].count("dismiss_fail") == 1, str(st5["log"]))
    check("也抛 GameGone", isinstance(err5, ga.GameGone), repr(err5))

    # ---------------------------------------------------------------- 用例6
    print("== 用例6：弹窗收掉但房间「开始游戏」没亮 → 不抛，继续等大厅 ==")
    st6 = install_popup({}, body=True, fail=False)
    ft6 = FakeTime()
    ga.time = ft6
    res6, err6 = run(make_bot(ft6, FakeVis(miss_n=20), room_state="busy"), timeout=30000)
    ga.time = real_time
    check("没抛 GameGone（继续等）", err6 is None, repr(err6))
    check("确实收过弹窗", st6["log"].count("dismiss") == 1, str(st6["log"]))
    check("最后等到了大厅按钮", res6 is not None, repr(res6))
    check("没重启平台", not restart_calls, f"重启 {len(restart_calls)} 次")

    # ---------------------------------------------------------------- 用例7
    print("== 用例7：弹窗 ESC/取消都关不掉 → 不重启平台、不抛异常 ==")
    st7 = install_popup({}, body=True, fail=False, close_on_dismiss=False)
    ft7 = FakeTime()
    ga.time = ft7
    res7, err7 = run(make_bot(ft7, FakeVis(miss_n=30), room_state="ready"), timeout=20000)
    ga.time = real_time
    check("没抛异常", err7 is None, repr(err7))
    check("没重启平台", not restart_calls, f"重启 {len(restart_calls)} 次")
    check("弹窗一直挂着 → 反复重试但仍等到按钮",
          st7["log"].count("dismiss") >= 2 and res7 is not None,
          f"dismiss {st7['log'].count('dismiss')} 次")

    # ---------------------------------------------------------------- 用例9
    print("== 用例9：房间窗确实没了 → 抛 RoomGone（交回 pipeline 重新建房）==")
    st9 = install_popup({}, body=True, fail=False)
    ft9 = FakeTime()
    ga.time = ft9
    res9, err9 = run(make_bot(ft9, FakeVis(miss_n=100000), room_state="gone"))
    ga.time = real_time
    check("抛的是 RoomGone", isinstance(err9, ga.RoomGone), repr(err9))
    check("RoomGone 是 GameGone 子类（pipeline 走的还是 gone 分支）",
          isinstance(err9, ga.GameGone))
    check("房间没了也不重启平台", not restart_calls, f"重启 {len(restart_calls)} 次")
    check("说明里写了「重新搜图建房」", "重新搜图建房" in (str(err9) or ""), str(err9))

    # ---------------------------------------------------------------- 用例8
    print("== 用例8：6 秒巡检（disc_interval_s）==")
    import os
    here = os.path.dirname(os.path.abspath(dg.__file__))
    src = open(os.path.join(here, "game_auto.py"), encoding="utf-8").read()
    check("默认间隔 6 秒", 'spec.get("disc_interval_s")' in src and "or 6" in src)
    cfg = open(os.path.join(here, "configs", "wuxia_lobby.json"), encoding="utf-8").read()
    check("大厅配置 disc_interval_s=6", '"disc_interval_s": 6' in cfg)
    pipe = open(os.path.join(here, "configs", "pipeline.json"), encoding="utf-8").read()
    check("pipeline 配了 rebuild_on_room_gone", '"rebuild_on_room_gone": 2' in pipe)
    check("pipeline 有重建逻辑", "断线弹窗收掉后房间窗已不存在" in src)

    if real_leave is not None:
        import room_finder
        room_finder.leave_room = real_leave
    ga.war3_alive = real_alive
    ga._restart_platform = real_restart
    print("== 汇总 ==", "全部通过" if not FAIL else f"失败 {FAIL}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
