# -*- coding: utf-8 -*-
"""
循环跑「开局 → 游戏中准备四步 → 退出游戏」N 轮

每轮：
  1) 开局     : game_auto.py pipeline  (房间在就跳过①②③，直接④开局⑤选难度)
  2) 等进游戏 : 截图探针 dark<0.35 视为进入地图（最多等 100 秒）
  3) 四步准备 : in_game.py            (①技能 ②装备 ③升级 ④自动挑战校验)
  4) 退出游戏 : war3_exit.json quit_game (F10→E→X→X，每步 3 秒)
  5) 等平台回到详情页，进入下一轮

两条自动兜底：
  · PlatWarnGuard：「您正在房间内…」弹窗 → 点「取消」保住房间
  · CheatGuard   ：弹「官方检测该账号有作弊风险…」→ **直接 taskkill 结束 War3**，
                   冷却后自动重开（不点任何按钮，点了也只是白点）

用法：
  python loop_round.py                        # 跑 3 轮
  python loop_round.py --rounds 5
  python loop_round.py --rounds 3 --skip-upgrade   # 跳过升级武器（省金币）
  python loop_round.py --retries 2 --cooldown 90   # 被风控打断后的重开次数/冷却秒数
  python loop_round.py --dry                  # 只打印计划不执行
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
import win32gui
import win32process
from PIL import ImageGrab

HERE = __file__.rsplit("\\", 1)[0] if "\\" in __file__ else "."
PY = sys.executable
SKILL_REG = (730, 108, 950, 400)     # 技能面板探针（有面板/黑屏都 >0.4）


class PlatWarnGuard(threading.Thread):
    """盯「平台提示：您正在房间内…」弹窗 → 点「取消」留在房间（绝不能点「确定」，那会解散房间）"""

    daemon = True

    def __init__(self, interval=1.0):
        super().__init__(name="plat-guard")
        self.interval = interval
        self._stop = threading.Event()
        self.hits = 0
        self.checks = 0

    def stop(self):
        self._stop.set()

    def run(self):
        import room_finder as rf
        while not self._stop.is_set():
            self.checks += 1
            try:
                if rf.handle_popup(prefer_cancel=True, verbose=False) == "clicked":
                    self.hits += 1
                    print("   (guard) ⚠️ 「您正在房间内…」弹窗 → 已点「取消」保留房间", flush=True)
                    time.sleep(1.2)
            except Exception:
                pass
            self._stop.wait(self.interval)


class CheatGuard(threading.Thread):
    """盯「官方检测该账号有作弊风险」提示 → 立刻 taskkill 结束 War3（不点按钮），置 tripped

    一发现就杀，是因为这时「游戏已结束」，四步全是空点；它只是把窗口停在丧气画面上。
    """

    daemon = True

    def __init__(self, interval=1.5, killer=None):
        super().__init__(name="cheat-guard")
        self.interval = interval
        self._stop = threading.Event()
        self.tripped = False        # 本轮是否被风控打断
        self.hits = 0
        self.checks = 0
        self._killer = killer       # 便于单测替换（默认 cheat_guard.kill_war3）

    def reset(self):
        self.tripped = False

    def stop(self):
        self._stop.set()

    def run(self):
        import cheat_guard as cg
        kill = self._killer or cg.kill_war3
        while not self._stop.is_set():
            self.checks += 1
            try:
                h, s = cg.detect()
                if h:
                    print(f"   (guard) ⚠️ 检测到「作弊风险」提示（score={s:.3f}，hwnd={h}）"
                          f" → 直接结束 War3 进程", flush=True)
                    kill()
                    self.hits += 1
                    self.tripped = True
                    time.sleep(2)
            except Exception:
                pass
            self._stop.wait(self.interval)


def run(cmd, timeout=600, label=""):
    t0 = time.time()
    print(f"\n▶ {label or ' '.join(cmd)}", flush=True)
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"   ⏱ {label} 超时 {timeout}s 被终止", flush=True)
        return 1, ""
    out = (p.stdout or "") + (p.stderr or "")
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-12:]
    for ln in tail:
        print("   " + ln, flush=True)
    print(f"   ⏱ {label} 用时 {time.time() - t0:.1f}s  (exit={p.returncode})", flush=True)
    return p.returncode, out


def find_war3():
    hits = []

    def cb(h, _):
        if win32gui.GetWindowText(h).lower().startswith("warcraft"):
            hits.append(h)

    win32gui.EnumWindows(cb, None)
    for h in hits:
        if win32gui.IsIconic(h):
            win32gui.ShowWindow(h, 9)      # SW_RESTORE
    return hits


def war3_client():
    """返回 (hwnd, x, y, w, h) 或 None"""
    import ctypes
    from ctypes import wintypes

    class RC(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    for h in find_war3():
        if win32gui.IsIconic(h):
            continue
        rc = RC()
        ctypes.windll.user32.GetClientRect(h, ctypes.byref(rc))
        if rc.r < 600 or rc.b < 400:
            continue
        pt = wintypes.POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(h, ctypes.byref(pt))
        return h, pt.x, pt.y, rc.r, rc.b
    return None


def dark_probe():
    info = war3_client()
    if not info:
        return None
    h, x, y, w, hh = info
    # 被平台窗口盖住时截到的是平台画面（dark 会飙到 0.9+），先确保游戏在前台
    try:
        if win32gui.GetForegroundWindow() != h:
            win32gui.ShowWindow(h, 9)
            win32gui.SetForegroundWindow(h)
            time.sleep(0.25)
            info = war3_client()
            if not info:
                return None
            h, x, y, w, hh = info
    except Exception:
        pass
    img = np.array(ImageGrab.grab(bbox=(x, y, x + w, y + hh)))
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    x0, y0, x1, y1 = SKILL_REG
    g = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    return float((g < 60).mean())


def wait_ingame(timeout=120, tag="", guard=None):
    """等进入地图。中途若被风控打断（guard.tripped）立刻返回 False"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if guard is not None and guard.tripped:
            print(f"   ⚠️ {tag} 等待中被风控打断", flush=True)
            return False
        d = dark_probe()
        if d is None:
            time.sleep(2)
            continue
        if d < 0.35:
            print(f"   ✅ {tag} 已进入地图 (dark={d:.2f}, 等待 {time.time() - t0:.0f}s)", flush=True)
            return True
        time.sleep(2)
    print(f"   ⚠️ {tag} {timeout}s 内没进地图（dark={dark_probe()}）", flush=True)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--skip-upgrade", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--only-start", action="store_true", help="不重开，直接从开局跑（平台已在详情页/房间）")
    ap.add_argument("--retries", type=int, default=2, help="被风控打断后，本轮最多重开几次（默认 2）")
    ap.add_argument("--cooldown", type=int, default=60, help="被风控打断后冷却多少秒再重开（默认 60）")
    ap.add_argument("--no-cheat-guard", action="store_true", help="关闭作弊提示看门狗（调试用）")
    args = ap.parse_args()

    print(f"计划：{args.rounds} 轮 [开局 → 四步 → 退出]，"
          f"风控重开上限 {args.retries} 次/轮，冷却 {args.cooldown}s", flush=True)
    if args.dry:
        return 0

    import room_finder as rf

    guard = PlatWarnGuard(interval=1.0)
    guard.start()
    print("🛡 平台弹窗看门狗已启动（「您正在房间内…」→ 自动点「取消」，保住房间）", flush=True)

    cheat = None
    if not args.no_cheat_guard:
        cheat = CheatGuard(interval=1.5)
        cheat.start()
        print("🛡 风控看门狗已启动（「作弊风险」提示 → 直接结束 War3，冷却后重开）", flush=True)

    rounds = []
    for i in range(1, args.rounds + 1):
        print(f"\n{'=' * 58}\n第 {i}/{args.rounds} 轮  {time.strftime('%H:%M:%S')}\n{'=' * 58}", flush=True)
        rt0 = time.time()
        rec = {"round": i, "重开": 0}
        played = False

        for attempt in range(args.retries + 1):
            if cheat is not None:
                cheat.reset()
            if attempt:
                print(f"\n   🔁 第 {i} 轮第 {attempt} 次重开（上一次被风控打断）", flush=True)
                rec["重开"] = attempt

            # ---- 0) 清场：上一轮可能没退干净 ----
            # 退出失败时 War3 会带着「结束游戏选项」面板留在最前，把房间窗口整个盖住。
            # 这时这轮开局截图房间区域，截到的是游戏画面 → 找不到「开始游戏」→ 白跑一轮
            # （2026-11-16 10:53 第 1 轮退出失败，第 2 轮就是这么被连累的）。
            if find_war3():
                print("   ⚠️ 发现 War3 仍在运行（上轮没退干净）→ 先退出再开局", flush=True)
                rec["清场"] = "有残留"
                run([PY, "game_auto.py", "run", "--config", "configs/war3_exit.json",
                     "--task", "quit_game", "--autostart"], label="⓪ 清场：退出残留 War3")
                time.sleep(1.5)
                if find_war3():
                    print("   ⛔ 正常退出没成功 → 直接结束 War3 进程", flush=True)
                    try:
                        import cheat_guard as cg
                        cg.kill_war3()
                    except Exception as e:
                        print(f"   (杀进程失败：{e})", flush=True)
                    time.sleep(2.0)
                if find_war3():
                    print("   ❌ War3 仍然在跑，本轮无法保证干净开局 → 跳过", flush=True)
                    rec["开局"] = "失败(War3 残留)"
                    break

            # ---- 1) 开局 ----
            # 先看是不是「已经建好房间」：房间是独立窗口，重复建房会弹「您正在房间内…」
            room = None
            try:
                room = rf.find_room(verbose=False)
            except Exception as e:
                print(f"   (房间探测失败，按无房间处理：{e})", flush=True)
            if room:
                print(f"   ℹ️ 已存在房间窗口 hwnd={room['hwnd']} "
                      f"({room['client'][2]}x{room['client'][3]}) → 跳过搜图/建房/填密码", flush=True)
                rec["房间"] = "复用已有"
            else:
                rec["房间"] = "新建"

            rc, _ = run([PY, "game_auto.py", "pipeline", "--pipeline", "configs/pipeline.json"],
                        label="① 开局（pipeline 5 步）")
            if cheat is not None and cheat.tripped:
                print(f"   ⛔ 开局阶段被风控打断 → 冷却 {args.cooldown}s 后重开", flush=True)
                time.sleep(args.cooldown)
                continue
            if rc != 0:
                print("   ❌ 开局失败，本轮跳过", flush=True)
                rec["开局"] = f"失败(exit={rc})"
                break
            rec["开局"] = "成功"

            # ---- 2) 等进游戏 ----
            rec["进游戏"] = "成功" if wait_ingame(120, "②", guard=cheat) else "超时/被打断"
            if cheat is not None and cheat.tripped:
                print(f"   ⛔ 载入阶段被风控打断 → 冷却 {args.cooldown}s 后重开", flush=True)
                time.sleep(args.cooldown)
                continue

            # ---- 3) 四步 ----
            cmd = [PY, "in_game.py"]
            if args.skip_upgrade:
                cmd += ["--skip", "3"]
            rc3, out3 = run(cmd, label="③ 游戏中准备四步")
            # 只取最后那四行计数（技能：2/3 这种）。逐轮明细会很长，
            # 之前汇总里塞了一整串「技能第1轮：…/技能第2轮：…」，看一眼就读不下去了；
            # 明细仍然在 logs 的完整输出里，需要时去翻。
            summary = [ln.strip() for ln in out3.splitlines()
                       if ln.strip().startswith(("技能：", "装备：", "升级：", "自动挑战："))]
            rec["四步"] = " / ".join(summary) if summary else f"exit={rc3}"
            if cheat is not None and cheat.tripped:
                print(f"   ⛔ 四步阶段被风控打断 → 冷却 {args.cooldown}s 后重开", flush=True)
                time.sleep(args.cooldown)
                continue

            played = True
            break

        # ---- 4) 退出（被风控杀掉的就不用退了）----
        if find_war3():
            rc4, _ = run([PY, "game_auto.py", "run", "--config", "configs/war3_exit.json",
                          "--task", "quit_game", "--autostart"], label="④ 退出游戏")
            time.sleep(3)
            gone = not find_war3()
            rec["退出"] = ("成功" if gone else "⚠️ 窗口还在") if rc4 == 0 else f"失败(exit={rc4})"
        else:
            rec["退出"] = "已在风控时结束"
        rec["耗时"] = f"{time.time() - rt0:.0f}s"
        rounds.append(rec)
        print(f"\n第 {i} 轮完成：{rec}", flush=True)
        time.sleep(4)

    guard.stop()
    if cheat is not None:
        cheat.stop()
    print(f"\n\n{'=' * 58}\n汇总（{len(rounds)} 轮）\n{'=' * 58}", flush=True)
    for r in rounds:
        print(f"第{r['round']}轮 | 房间 {r.get('房间','-')} | 开局 {r.get('开局','-')} | "
              f"进游戏 {r.get('进游戏','-')} | 退出 {r.get('退出','-')} | "
              f"重开 {r.get('重开',0)} | {r.get('耗时','-')}", flush=True)
        print(f"      四步 → {r.get('四步','-')}", flush=True)
    print(f"\n🛡 平台弹窗看门狗点掉 {guard.hits} 次（扫描 {guard.checks} 次）", flush=True)
    if cheat is not None:
        print(f"🛡 风控看门狗结束 War3 {cheat.hits} 次（扫描 {cheat.checks} 次）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
