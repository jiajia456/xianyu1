# -*- coding: utf-8 -*-
"""Win7 房间窗复用 / 平台重启策略 离线测试（2026-11-27）

不用开游戏、不用等 15 分钟。跑：
    py38\\python.exe test_win7_room_reuse.py

覆盖 4 个分支：
  1) Win7 模板认不出（截图全 0）但房间窗按 PID+尺寸存在 → 复用绑定、返回 ok，不重启 KK、不置顶
  2) 房间明确「游戏中」（开始游戏不可用 + 有退出按钮）→ 重启 KK
  3) 第 ④ 步在已绑定房间窗上开局失败 → RC_RESTARTED（重启次数由上层限制）
  4) KK 重启后需要登录 → 返回 login，不自动登录
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import game_auto as ga
import plat_restart as pr
import room_finder as rf
import sysenv

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


def fake_window(hwnd, pid, w, h):
    return {"hwnd": hwnd, "pid": pid, "client": (0, 0, w, h),
            "title": "KK官方对战平台", "iconic": False}


def patch(**kw):
    """临时替换模块属性，返回 restore 函数"""
    saved = []
    owners = {"ga": ga, "rf": rf, "se": sysenv, "pr": pr}
    for k, v in kw.items():
        mod, attr = k.split("__", 1)
        o = owners[mod]
        saved.append((o, attr, getattr(o, attr, None), hasattr(o, attr)))
        setattr(o, attr, v)

    def restore():
        for o, attr, old, had in reversed(saved):
            if had:
                setattr(o, attr, old)
            else:
                try:
                    delattr(o, attr)
                except Exception:
                    pass
    return restore


def reset_globals():
    ga.BOUND_ROOM.update({"hwnd": None, "via": None, "scores": None})
    ga.PREFER_HWND["hwnd"] = None
    ga.ROOM_BLOCKED["hwnd"] = 0


def case1_win7_blank_but_room_exists():
    print("\n[1] Win7 截图全 0，但房间窗按 PID+尺寸存在")
    reset_globals()
    calls = {"activate": 0, "restart": 0, "find_room": 0}

    def _no_restart(*a, **k):
        calls["restart"] += 1
        return {"status": "ok"}

    def _activate(*a, **k):
        calls["activate"] += 1

    def _find_room(**k):
        calls["find_room"] += 1
        raise AssertionError("已按 PID+尺寸找到房间，不应再逐窗置顶截图")

    windows = [fake_window(100, 7001, 1332, 945), fake_window(200, 7001, 1224, 904)]
    undo = patch(se__is_win7=lambda: True,
                 rf__find_room=_find_room,
                 rf__platform_windows=lambda **k: windows,
                 rf__is_room_busy=lambda r: False,
                 rf__activate=_activate,
                 ga___restart_platform=_no_restart)
    try:
        res = ga.verify_room_before_start(verbose=False)
    finally:
        undo()
    ck("返回 ok（复用房间，不重建）", res == "ok", "got=%r" % (res,))
    ck("绑定的就是 1224x904 那扇房间窗", ga.BOUND_ROOM.get("hwnd") == 200,
       "got=%r" % (ga.BOUND_ROOM.get("hwnd"),))
    ck("没有重启 KK", calls["restart"] == 0, "restart=%d" % calls["restart"])
    ck("没有反复置顶窗口", calls["activate"] == 0, "activate=%d" % calls["activate"])
    ck("没有调用逐窗截图识别", calls["find_room"] == 0,
       "find_room=%d" % calls["find_room"])


def case1b_win10_pair_skips_window_toggling():
    print("\n[1b] Win10 两扇 KK 窗口尺寸和 PID 匹配时也不逐窗置顶")
    reset_globals()
    calls = {"find_room": 0, "activate": 0}

    def _find_room(**kw):
        calls["find_room"] += 1
        raise AssertionError("已确认窗口配对，不应逐窗截图")

    def _activate(*a, **kw):
        calls["activate"] += 1

    windows = [fake_window(101, 7002, 1332, 945), fake_window(201, 7002, 1224, 904)]
    undo = patch(se__is_win7=lambda: False,
                 rf__find_room=_find_room,
                 rf__platform_windows=lambda **kw: windows,
                 rf__is_room_busy=lambda room: False,
                 rf__activate=_activate)
    try:
        res = ga.verify_room_before_start(verbose=False)
    finally:
        undo()
    ck("Win10 复用房间", res == "ok" and ga.BOUND_ROOM.get("hwnd") == 201)
    ck("Win10 不逐窗截图/置顶", calls == {"find_room": 0, "activate": 0}, str(calls))


def case2_room_busy_restarts():
    print("\n[2] 房间明确显示「游戏中」（开始游戏不可用 + 有退出）")
    reset_globals()
    calls = {"restart": 0}

    def _restart(*a, **k):
        calls["restart"] += 1
        return {"status": "ok"}

    windows = [fake_window(100, 7001, 1332, 945), fake_window(200, 7001, 1224, 904)]
    undo = patch(se__is_win7=lambda: True,
                 rf__find_room=lambda **k: None,
                 rf__platform_windows=lambda **k: windows,
                 rf__is_room_busy=lambda r: True,
                 ga___restart_platform=_restart)
    try:
        res = ga.verify_room_before_start(verbose=False)
    finally:
        undo()
    ck("判定为 platform_restarted", res == "platform_restarted", "got=%r" % (res,))
    ck("确实重启了 1 次", calls["restart"] == 1, "restart=%d" % calls["restart"])


def case3_step4_fail_no_restart():
    print("\n[3] ④ 开局失败不再重启平台（2026-11-27 16:54 用户定）")
    src = open(os.path.join(HERE, "game_auto.py"), encoding="utf-8").read()
    ck("已删除 _step_fail_needs_platform_restart", "_step_fail_needs_platform_restart" not in src)
    ck("已删除 _restart_after_step_fail", "_restart_after_step_fail" not in src)
    ck("失败分支直接 return 1",
       'if status == "failed":' in src and "④ 开局失败不再重启平台" in src)


def case8_ensure_kk_ready():
    print("\n[8] 启动时 KK 未运行 / 登录页")
    undo = patch(pr__ensure_running=lambda **k: {"status": "ok"})
    try:
        ck("平台就绪 → 继续（None）", ga._ensure_kk_ready() is None)
    finally:
        undo()
    undo = patch(pr__ensure_running=lambda **k: {"status": "login"})
    try:
        ck("登录页 → 停手 RC_LOGIN", ga._ensure_kk_ready() == ga.RC_LOGIN)
    finally:
        undo()
    undo = patch(pr__ensure_running=lambda **k: {"status": "noexe"})
    try:
        ck("找不到 KK → 返回 1", ga._ensure_kk_ready() == 1)
    finally:
        undo()


def case4_restart_requires_login():
    print("\n[4] KK 重启后需要登录")
    reset_globals()
    undo = patch(se__is_win7=lambda: True,
                 rf__find_room=lambda **k: None,
                 rf__platform_windows=lambda **k: [fake_window(100, 7001, 1332, 945),
                                                   fake_window(200, 7001, 1224, 904)],
                 rf__is_room_busy=lambda r: True,
                 ga___restart_platform=lambda *a, **k: {"status": "login"})
    try:
        res = ga.verify_room_before_start(verbose=False)
    finally:
        undo()
    ck("返回 login（停手等手动登录）", res == "login", "got=%r" % (res,))


def case9_restart_limited():
    print("\n[9] 同一轮重启次数受限（防止无限重启）")
    src = open(os.path.join(HERE, "full_loop.py"), encoding="utf-8").read()
    ck("有 used_platform 计数", "used_platform += 1" in src)
    ck("超过 1 次就停", "used_platform > 1" in src)
    ck("重启上限来自 restart_on_enter_fail",
       'max_platform = 1 if cfg.get("restart_on_enter_fail", True) else 0' in src)


def main():
    print("=" * 60)
    print("Win7 房间窗复用 / 重启策略 离线测试")
    print("=" * 60)
    case1_win7_blank_but_room_exists()
    case1b_win10_pair_skips_window_toggling()
    case2_room_busy_restarts()
    case4_restart_requires_login()
    case3_step4_fail_no_restart()
    case8_ensure_kk_ready()
    case9_restart_limited()
    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
