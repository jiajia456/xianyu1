# -*- coding: utf-8 -*-
"""v1.17 离线自检：窗口抓不到画面时「不提前退出，耗到 timeout 上限」

背景（2026-11-18 20:55 用户定）：
  那台电脑点「开始游戏」后 War3 在 91 秒时窗口变 0x0（进程退了），
  程序按设计继续等，但 1 秒空抓一次没意义。用户要求：
    · **不许提前退出**，一直耗到 900 秒上限；
    · 抓不到画面就 **30 秒重抓一次**；
    · 但仍然要能立刻接上「War3 重建窗口后画出大厅」那种情况。

三个断言：
  ① 窗口一直抓不到 → 不提前退出，跑满 timeout 才报超时，且抓取次数被节流到 1 次。
  ② 窗口能抓到（只是画面不是大厅）→ 仍按 interval 高频抓，不慢。
  ③ 窗口先死、2 秒后新窗口画出大厅 → 靠 any_window 扫描接上，不受 30 秒节流拖累。
     （17:14 定双速：抓不到/黑屏回 2 秒；常态巡检 17:14 定 15 秒、17:44 改 30 秒，本例走 2 秒那条）
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import cv2                                                    # noqa: E402
import numpy as np                                            # noqa: E402
import game_auto as ga                                        # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + extra) if extra else ""))
    ok = ok and bool(cond)


TPL = cv2.imread("templates/btn_lobby_start.png")
assert TPL is not None, "模板缺失：btn_lobby_start.png"


def canvas(w, h, paste=False):
    img = np.zeros((h, w, 3), np.uint8)
    if paste:
        th, tw = TPL.shape[:2]
        y, x = h // 2, max(0, w // 2 - tw // 2)
        img[y:y + th, x:x + tw] = TPL
    return img


class W:
    def __init__(self, hwnd, size, alive=True):
        self.hwnd = hwnd
        self.pid = 3388
        self.min_size = 400
        self._size = size
        self._alive = alive

    def alive(self):
        return self._alive

    def client_rect(self):
        # 窗口死了/最小化 → 客户区 0x0（和那台电脑日志里一样）
        return (0, 0, 0, 0) if not self._alive else (0, 0, self._size[0], self._size[1])

    def fg_same_process(self):
        return True

    def focus(self, *a, **k):
        return True


class Cap:
    """dead=True 时抓图抛异常（= 窗口 0x0 / 已消失），并统计被调用次数"""

    def __init__(self, dead=True, shots=None, t0=None):
        self.dead = dead
        self.shots = shots or {}
        self.grabs = 0
        self.t0 = t0

    def grab(self, win):
        self.grabs += 1
        if self.dead:
            raise RuntimeError("客户区 0x0，抓不到画面")
        return self.shots.get(win.hwnd, canvas(1264, 921, paste=False))


def make_bot(cap, win):
    bot = ga.Bot.__new__(ga.Bot)
    bot.cfg_dir = os.getcwd()
    bot.cap = cap
    bot.vis = ga.Vision(os.path.join(os.getcwd(), "templates"))
    bot.win = win
    bot._cap_ref = [1690, 921]
    bot._wait_spec = None
    bot._stop = threading.Event()
    bot._paused = threading.Event()
    bot._paused.set()
    bot._last_fg = 0.0
    bot._last_dead_log = 0.0
    bot._checkpoint = lambda: None          # 只看等图循环本身，旁路暂停/抢前台
    bot._sleep = lambda ms: time.sleep(ms / 1000.0)
    bot._dump_miss = lambda t: None
    return bot


SPEC = {"template": "btn_lobby_start.png", "threshold": 0.85,
        "interval": 200, "any_window": True, "required": True}

real_enum = ga.enum_windows_full

print("① 窗口一直是 0x0：不许提前退出，跑满 timeout 才报超时，抓取被节流")
try:
    ga.enum_windows_full = lambda: []       # 没有任何候选窗口
    cap = Cap(dead=True)
    bot = make_bot(cap, W(197750, (0, 0), alive=False))
    spec = dict(SPEC, timeout=3000)
    t0 = time.time()
    raised = None
    try:
        bot._wait_image_loop(spec)
    except Exception as e:
        raised = e
    cost = time.time() - t0
    check("没有提前退出（抛的是「等待超时」）",
          raised is not None and "等待超时" in str(raised), "raised=%r" % (raised,))
    check("真的等满了 timeout（≥2.9s）", cost >= 2.9, "cost=%.2fs" % cost)
    check("空抓被节流到 1 次（30 秒才重抓）", cap.grabs == 1, "grabs=%d" % cap.grabs)
finally:
    ga.enum_windows_full = real_enum

print()
print("② 窗口能抓到、只是画面不是大厅：仍按 interval 高频抓（不能被 30 秒拖慢）")
try:
    ga.enum_windows_full = lambda: []
    cap = Cap(dead=False, shots={197750: canvas(1264, 921, paste=False)})
    bot = make_bot(cap, W(197750, (1264, 921)))
    spec = dict(SPEC, timeout=2000)
    raised = None
    try:
        bot._wait_image_loop(spec)
    except Exception as e:
        raised = e
    check("超时退出", raised is not None and "等待超时" in str(raised))
    check("抓取次数够多（≥6 次 / 2 秒，interval=200ms）", cap.grabs >= 6, "grabs=%d" % cap.grabs)
finally:
    ga.enum_windows_full = real_enum

print()
print("③ 窗口先死、2 秒后新窗口画出大厅：any_window 1 秒扫描接上，不等 30 秒")
try:
    NEW = {"hwnd": 222333, "pid": 3388, "title": "我在峡谷打僵尸 [ by:少女梦 ] 11-15",
           "cls": "TkTopLevel", "exe": "", "w": 1264, "h": 921,
           "offscreen": False, "iconic": False}
    t_start = time.time()

    class CapTimed:
        """前 2 秒抓不到（窗口死了），之后新窗口能抓到大厅画面"""

        def __init__(self):
            self.grabs = 0

        def grab(self, win):
            self.grabs += 1
            if win.hwnd == NEW["hwnd"]:
                return canvas(1264, 921, paste=True)
            raise RuntimeError("窗口 0x0")

    cap = CapTimed()
    bot = make_bot(cap, W(197750, (0, 0), alive=False))
    ga.enum_windows_full = (lambda: [] if time.time() - t_start < 2.0 else [NEW])
    spec = dict(SPEC, timeout=30000)
    hit = bot._wait_image_loop(spec)
    cost = time.time() - t_start
    check("最终接上了新窗口", hit is not None and bot.win.hwnd == NEW["hwnd"],
          "hwnd=%s" % bot.win.hwnd)
    check("接上的时间 < 6 秒（没被 30 秒节流拖住）", cost < 6.0, "cost=%.2fs" % cost)
finally:
    ga.enum_windows_full = real_enum

print()
print("④ 旧窗口还活着、但画面是纯黑（空窗口残留）：也要 2 秒内切到新窗口")
try:
    NEW2 = {"hwnd": 333444, "pid": 3388, "title": "我在峡谷打僵尸 [ by:少女梦 ] 11-15",
            "cls": "TkTopLevel", "exe": "", "w": 1264, "h": 921,
            "offscreen": False, "iconic": False}
    t_start = time.time()

    class CapBlank:
        """旧窗口一直抓得到画面，但是全黑；新窗口画出大厅"""

        def __init__(self):
            self.grabs = 0
            self.old_grabs = 0

        def grab(self, win):
            self.grabs += 1
            if win.hwnd == NEW2["hwnd"]:
                return canvas(1264, 921, paste=True)
            self.old_grabs += 1
            return canvas(1264, 921, paste=False)     # 全黑，std=0

    cap = CapBlank()
    bot = make_bot(cap, W(197750, (1264, 921), alive=True))   # 旧窗口活着
    ga.enum_windows_full = (lambda: [] if time.time() - t_start < 2.0 else [NEW2])
    spec = dict(SPEC, timeout=30000)
    hit = bot._wait_image_loop(spec)
    cost = time.time() - t_start
    check("最终接上了新窗口", hit is not None and bot.win.hwnd == NEW2["hwnd"],
          "hwnd=%s" % bot.win.hwnd)
    check("接上的时间 < 6 秒（空画面不按 30 秒巡检）", cost < 6.0, "cost=%.2fs" % cost)
finally:
    ga.enum_windows_full = real_enum

print()
print("全部通过" if ok else "❌ 有用例失败")
sys.exit(0 if ok else 1)
