# -*- coding: utf-8 -*-
"""离线验证：pipeline 每一步**只跑一次、按顺序推进**。

为什么要有这个自检：2026-11-18 23:14 实测日志里，pipeline 从 23:14:56 到 23:17:07
一直重复第 1 步（每轮 12 秒，永远不进第 2 步）—— 把 `for i, st in enumerate(steps)`
改造成 `while idx < len(steps)` 时漏了在循环末尾 `idx += 1`，for 循环自带的推进没了。

这个自检用假 Bot 把整条 cmd_pipeline 跑一遍，只关心「跑了哪几步、各几次」，
不碰真窗口、不点鼠标。
"""
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import game_auto as ga

FAIL = []


def check(cond, msg, extra=""):
    print(("   ✅ " if cond else "   ❌ ") + msg + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(msg)


class FakeInput:
    def release_all(self):
        pass


class FakeBot:
    """假 Bot：run_task 只记一笔，可按 task 名抛 GameGone 模拟「这一局已结束」。"""

    calls = []
    gone_tasks = set()
    gone_cls = None

    def __init__(self, cfg, base, dry_run=False):
        self.input = FakeInput()

    def bind_hotkeys(self):
        pass

    def run_task(self, task):
        FakeBot.calls.append(task)
        if task in FakeBot.gone_tasks:
            cls = FakeBot.gone_cls or ga.GameGone
            raise cls("模拟：这一局已经结束")


def run_pipe(steps, reopen=2, gone_tasks=(), rebuild=2, gone_cls=None):
    """把 steps 写成临时 pipeline，跑一遍 cmd_pipeline，返回实际执行顺序。"""
    FakeBot.calls = []
    FakeBot.gone_tasks = set(gone_tasks)
    FakeBot.gone_cls = gone_cls
    d = tempfile.mkdtemp(prefix="kkpipe_")
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    p = os.path.join(d, "pipeline.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"steps": steps, "reopen_on_game_gone": reopen,
                   "rebuild_on_room_gone": rebuild}, f, ensure_ascii=False)

    args = types.SimpleNamespace(
        pipeline=p, only="", dry_run=False,
        no_room_check=True, no_map_update=True,
        busy_wait=None, no_busy_wait=True,
    )

    old = (ga.Bot, ga._ad_check, ga._busy_hit, ga.verify_room_before_start,
           ga.create_dialog_hwnd, ga._open_create_dialog, ga._ensure_kk_ready,
           ga.await_room_front)
    ga.Bot = FakeBot
    ga._ensure_kk_ready = lambda: None      # 2026-11-27：这条检查会真去等平台窗口，测试里跳过
    ga.await_room_front = lambda: (None, 0.0)   # 新窗口绑定要静默 6s，离线测试直接跳过
    ga._ad_check = lambda: None
    ga._busy_hit = lambda: None
    ga.verify_room_before_start = lambda: None
    ga.create_dialog_hwnd = lambda *a, **k: 1
    ga._open_create_dialog = lambda *a, **k: True
    import map_update as mu
    import room_finder as _rf_mod
    real_find = mu.find
    real_fr, real_act = _rf_mod.find_room, _rf_mod.activate
    mu.find = lambda verbose=False: None
    # 房间探测：给一个「房间在、开始游戏亮着」的假房间，避免离线环境没有真房间
    _rf_mod.find_room = lambda verbose=False, restore=True: {
        "hwnd": 123456, "client": (0, 0, 1224, 904), "title": "KK官方对战平台",
        "roomno_score": 1.0, "start_score": 0.99, "quit_score": 0.0}
    _rf_mod.activate = lambda *a, **k: None
    ga.BOUND_ROOM.clear()
    try:
        rc = ga.cmd_pipeline(args)
    finally:
        (ga.Bot, ga._ad_check, ga._busy_hit, ga.verify_room_before_start,
         ga.create_dialog_hwnd, ga._open_create_dialog, ga._ensure_kk_ready,
         ga.await_room_front) = old
        mu.find = real_find
        _rf_mod.find_room, _rf_mod.activate = real_fr, real_act
    return rc, list(FakeBot.calls)


CFG = "configs/wuxia_lobby.json"     # 只用来过「配置文件存在」这一关，内容不参与


def mk(name, task, **kw):
    d = {"name": name, "config": CFG, "task": task}
    d.update(kw)
    return d


print("== 用例1：4 步各跑一次，顺序不乱（这就是今晚那个死循环的直接回归） ==")
steps = [mk("① 甲", "t1"), mk("② 乙", "t2"), mk("③ 丙", "t3"), mk("④ 丁", "t4")]
rc, calls = run_pipe(steps)
check(rc == 0, "返回值 0", f"rc={rc}")
check(calls == ["t1", "t2", "t3", "t4"], "执行顺序 t1→t2→t3→t4，且各 1 次",
      f"实际 {calls}")

print("== 用例2：第 1 步不许重复（踩过的坑：① 连跑 12 轮不往下走） ==")
check(calls.count("t1") == 1, "t1 只跑 1 次", f"实际 {calls.count('t1')} 次")
check(len(calls) == 4, "总共只跑 4 次，没有空转", f"实际 {len(calls)} 次")

print("== 用例3：带 ③ 的步骤名也正常推进（会走「补点创建房间」那条前置检查） ==")
steps3 = [mk("① 搜地图并进详情页", "a"), mk("② 建房", "b"),
          mk("③ 填密码", "c"), mk("④ 房间开局", "d")]
rc, calls3 = run_pipe(steps3)
check(calls3 == ["a", "b", "c", "d"], "4 步各 1 次", f"实际 {calls3}")

print("== 用例4：GameGone 回退后不超次数、不死循环 ==")
steps4 = [mk("① 搜地图", "a"), mk("④ 房间开局", "d", activate_room=True), mk("⑤ 大厅", "e")]
rc, calls4 = run_pipe(steps4, reopen=1, gone_tasks={"e"})
# 正常：a → d → e(gone, 回退到 d) → d → e(gone, 次数用完 → 失败)
check(rc == 1, "重开次数用完 → 判失败 rc=1", f"rc={rc}")
check(calls4 == ["a", "d", "e", "d", "e"], "回退一次后仍按顺序，不会原地打转",
      f"实际 {calls4}")

print("== 用例6：房间窗没了（RoomGone）→ 从 ① 重新搜图建房，用完次数才失败 ==")
rc6, calls6 = run_pipe(steps4, reopen=2, rebuild=1,
                       gone_tasks={"e"}, gone_cls=ga.RoomGone)
check(rc6 == 1, "重建次数用完 → 判失败 rc=1", f"rc={rc6}")
check(calls6 == ["a", "d", "e", "a", "d", "e"],
      "先从 ① 重建一次，第二次 RoomGone 才失败", f"实际 {calls6}")

print("== 用例7：回不到 ④（本次链路没有开局步骤）→ 也走重建 ==")
steps7 = [mk("① 搜地图", "a"), mk("⑤ 大厅", "e")]
rc7, calls7 = run_pipe(steps7, reopen=2, rebuild=1, gone_tasks={"e"})
check(rc7 == 1, "重建次数用完 → 判失败 rc=1", f"rc={rc7}")
check(calls7 == ["a", "e", "a", "e"], "没有 ④ 时从 ① 重建一次再失败", f"实际 {calls7}")

print("== 用例5：防死循环的兜底还在（以后又漏推进也不会空转一晚上） ==")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_auto.py"),
           encoding="utf-8").read()
check("loops > max(20, len(steps) * 10)" in src, "有循环次数上限兜底")
i1 = src.find("idx += 1\n        done += 1")
check(i1 > 0, "正常路径上 idx 推进紧挨着 done += 1（位置正确）")

print("== 汇总 ==", "全部通过" if not FAIL else f"失败 {len(FAIL)} 项：{FAIL}")
sys.exit(0 if not FAIL else 1)
