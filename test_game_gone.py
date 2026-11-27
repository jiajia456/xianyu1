# -*- coding: utf-8 -*-
"""v1.22 自检：断线重连关掉之后「这一局已经结束」→ 立刻收手回退重开

背景（用户 2026-11-18 23:05，本机实测）：
  手动结束 War3 进程 → 平台弹「断线重连」→ 程序点「取消」是对的，
  但接着继续等一个永远不会出现的大厅按钮，白耗 15 分钟。

改法（2026-11-27 16:20 用户定的最终规则）：
  · 弹出「断线重连 / 断线重连失败」→ 先 ESC、关不掉再点「取消」/「确定」收掉弹窗；
    收掉后看房间窗「开始游戏」是否亮着：亮着 = 可以直接重开 → 抛 GameGone
    （不重启平台）；没亮 = 平台还认为在游戏中 → 继续等大厅。
  · 平台**没弹**窗、War3 进程却没了（或点了开始游戏一直没进程）→ 进程看门狗也能抛
    GameGone（窗口 0x0 但进程还在＝载入中，照旧继续等）。
  · pipeline 捕获 GameGone → 回退到「房间开局」那一步重新点「开始游戏」，
    次数由 pipeline.json 的 reopen_on_game_gone 控制（默认 2）。

断言：
  ① 弹窗 + War3 进程不在 → 抛 GameGone（不再空等）；
  ② 弹窗 + War3 进程还在 → **照样**抛 GameGone（弹窗本身就是「已结束」的证据）；
  ③ _reopen_target 能找回 ④（activate_room），只跑 ⑤ 时返回 None；
  ④ war3_alive 按进程名判定（war3.exe 算、python.exe 不算）。
"""
import os
import sys

import game_auto as ga
import disc_guard as dg

FAIL = []


def check(name, cond, extra=""):
    print("   ", "PASS" if cond else "FAIL", name, extra)
    if not cond:
        FAIL.append(name)


class FakeTime:
    def __init__(self, t=1000.0):
        self.t = float(t)

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)

    def strftime(self, fmt="%H:%M:%S"):
        return "23:05:00"


class FakeCap:
    def __init__(self):
        import numpy as np
        self._img = np.zeros((921, 1690, 3), "uint8")

    def grab(self, win):
        return self._img


class FakeWin:
    hwnd = 1

    def client_rect(self):
        return (0, 0, 1690, 921)


class FakeVis:
    def __init__(self, miss_n):
        self.miss_n = miss_n
        self.calls = 0

    def find_best(self, tpl, img, th, region, scales):
        self.calls += 1
        if self.calls <= self.miss_n:
            return None, None
        return "btn_lobby_start.png", (860, 600, 180, 46, 0.93)


def make_bot(ft, vis):
    bot = object.__new__(ga.Bot)
    bot.win = FakeWin()
    bot.cap = FakeCap()
    bot.vis = vis
    bot._sleep = lambda ms: ft.sleep(float(ms) / 1000.0)
    bot._checkpoint = lambda: None
    bot._scales_with_ref = lambda spec: [1.0]
    bot._bind_by_content = lambda spec: False
    bot._dump_miss = lambda t: None
    bot._wait_spec = None
    bot._dg = dg
    bot.cfg = {}
    # 本文件聚焦「进程没了要不要收手」，房间是否可开局固定为「亮着」
    bot._room_state = lambda: "ready"
    return bot


HIT = dict(hwnd=4243, w=480, h=300, title="KK官方对战平台",
           score=0.963, scale=1.0, tl=(56, 110), origin=(600, 300), area=480 * 300)
SPEC = {"template": "btn_lobby_start.png", "threshold": 0.85,
        "interval": 200, "timeout": 900000, "required": True}


def main():
    real_time = ga.time
    real_alive = ga.war3_alive
    clicks = []

    # 弹窗替身：ESC/取消 之后弹窗真的消失（否则会被判成「还挂着」而一直重试）
    popup = {"open": True}

    def _find(verbose=False, popup_only=True):
        return HIT if popup["open"] else None

    def _dismiss(verbose=True, dry=False, tries=3):
        clicks.append(1)
        popup["open"] = False
        return "esc"

    dg.find = _find
    dg.find_fail = lambda verbose=False, popup_only=True: None
    dg.dismiss = _dismiss
    dg.dismiss_fail = lambda verbose=True, dry=False, tries=3: "clicked"
    dg._log_event = lambda *a, **k: {}

    print("== 用例1：弹窗处理过 + War3 进程不在 → 抛 GameGone，不再空等 ==")
    ga.war3_alive = lambda: False
    ft = FakeTime()
    ga.time = ft
    bot = make_bot(ft, FakeVis(miss_n=100000))
    raised, cost = None, 0.0
    try:
        bot._wait_image_loop(dict(SPEC))
    except ga.GameGone as e:
        raised = e
    except Exception as e:                       # 其它异常都算错（会是超时）
        raised = e
    cost = ft.t - 1000.0
    ga.time = real_time
    check("抛的是 GameGone", isinstance(raised, ga.GameGone), repr(raised))
    check("弹窗只收 1 次（ESC/取消）", len(clicks) == 1, f"{len(clicks)} 次")
    check("没有干等到 900 秒（< 40 秒就收手）", cost < 40, f"cost={cost:.0f}s")

    print("== 用例2：弹窗出现 + War3 进程还在 → 房间可开局就照样收手重开 ==")
    # 2026-11-27 16:20 规则：弹窗收掉后看房间「开始游戏」——亮着就回 ④ 重开，
    # 不看 War3 进程（进程在也照样收手，房间还在就不用重启平台）。
    ga.war3_alive = lambda: True
    clicks.clear()
    popup["open"] = True          # 新一局，弹窗重新出现
    ft2 = FakeTime()
    ga.time = ft2
    bot2 = make_bot(ft2, FakeVis(miss_n=30))
    raised2, cost2 = None, 0.0
    try:
        bot2._wait_image_loop(dict(SPEC))
    except ga.GameGone as e:
        raised2 = e
    except Exception as e:
        raised2 = e
    cost2 = ft2.t - 1000.0
    ga.time = real_time
    check("进程还在也抛 GameGone", isinstance(raised2, ga.GameGone), repr(raised2))
    check("关过 1 次弹窗就收手（不去点大厅按钮）", len(clicks) == 1, f"{len(clicks)} 次")
    check("几秒内收手，不是等到按钮（< 40 秒）", cost2 < 40, f"cost={cost2:.0f}s")

    print("== 用例2b：平台**没弹**断线重连，War3 进程直接没了（23:26 实测）→ 也要收手 ==")
    # 用户这次是在任务管理器里结束 War3，平台直接回房间、一个弹窗都没有，
    # 旧逻辑只能对着 0x0 的窗口干等 900 秒。看门狗：先见过进程活着，之后连续 2 次
    # 查不到就判「这一局结束」。
    dg.find = lambda verbose=False, popup_only=True: None
    clicks.clear()
    state = {"n": 0}

    def fake_alive_once():
        state["n"] += 1
        return state["n"] <= 1          # 第 1 次查还活着，之后就没了

    ga.war3_alive = fake_alive_once
    ft3 = FakeTime()
    ga.time = ft3
    bot3 = make_bot(ft3, FakeVis(miss_n=100000))
    raised3 = None
    try:
        bot3._wait_image_loop(dict(SPEC, proc_watch=True))
    except ga.GameGone as e:
        raised3 = e
    except Exception as e:
        raised3 = e
    cost3 = ft3.t - 1000.0
    ga.time = real_time
    check("没有弹窗也抛 GameGone", isinstance(raised3, ga.GameGone), repr(raised3))
    check("一会儿就收手（< 90 秒，不是 900 秒）", cost3 < 90, f"cost={cost3:.0f}s")
    check("没有误点任何东西", len(clicks) == 0, f"{len(clicks)} 次")

    print("== 用例2c：没开 proc_watch 的等图步骤不受影响（进程不在也不该乱判）==")
    ga.war3_alive = lambda: False
    ft4 = FakeTime()
    ga.time = ft4
    bot4 = make_bot(ft4, FakeVis(miss_n=100000))
    r4, err4 = "未跑", None
    try:
        r4 = bot4._wait_image_loop(dict(SPEC, timeout=30000, required=False,
                                        interval=1000, proc_watch=False))
    except Exception as e:
        err4 = e
    ga.time = real_time
    check("没抛 GameGone，只是正常超时返回 None",
          err4 is None and r4 is None, f"{r4!r} / {err4!r}")

    print("== 用例2d：点了开始游戏 150s 连进程都不起来（秒退）→ 判这一局结束 ==")
    ga.war3_alive = lambda: False
    ft5 = FakeTime()
    ga.time = ft5
    bot5 = make_bot(ft5, FakeVis(miss_n=100000))
    raised5 = None
    try:
        bot5._wait_image_loop(dict(SPEC, proc_watch=True, spawn_wait_s=60))
    except ga.GameGone as e:
        raised5 = e
    except Exception as e:
        raised5 = e
    cost5 = ft5.t - 1000.0
    ga.time = real_time
    check("秒退也抛 GameGone", isinstance(raised5, ga.GameGone), repr(raised5))
    check("在 spawn_wait_s 附近收手", 55 <= cost5 <= 95, f"cost={cost5:.0f}s")

    dg.find = lambda verbose=False, popup_only=True: HIT

    print("== 用例3：_reopen_target 找回「房间开局」那一步 ==")
    steps = [
        {"name": "① 搜地图并进详情页", "skip_if_room": True},
        {"name": "② 创建房间", "skip_if_room": True},
        {"name": "③ 填密码并提交", "skip_if_room": True},
        {"name": "④ 房间内开局", "activate_room": True},
        {"name": "⑤ 大厅选最高难度并开打"},
    ]
    check("⑤ 回退到 ④", ga._reopen_target(steps, 4) == 3, str(ga._reopen_target(steps, 4)))
    check("只跑 ⑤ 时找不到可回退步骤", ga._reopen_target(steps[4:], 0) is None)
    check("④ 自己抛时回退目标就是自己（重试 ④，次数由 reopen_on_game_gone 兜住）",
          ga._reopen_target(steps, 3) == 3, str(ga._reopen_target(steps, 3)))

    print("== 用例4：war3_alive 按进程名判定 ==")
    import os as _os
    name = ga._pid_exe_name(_os.getpid())
    check("取得到自己的进程名", name.endswith("python.exe"), name)
    real_pid = ga._pid_exe_name
    ga.war3_alive = real_alive          # 用例2b~2d 换过它，这里必须先还原
    ga._pid_exe_name = lambda pid: "war3.exe" if pid and pid > 0 else ""
    alive_fake = ga.war3_alive()
    ga._pid_exe_name = real_pid
    ga.war3_alive = real_alive
    check("进程名是 war3.exe 时判定为活着", alive_fake is True, str(alive_fake))

    print("== 汇总 ==", "全部通过" if not FAIL else f"失败 {FAIL}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
