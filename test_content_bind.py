# -*- coding: utf-8 -*-
"""v1.14 离线自检：按「画面内容」找窗口（用户 2026-11-18 18:43 提的思路）

核心断言：
  1) 谁画出大厅开始按钮，就绑谁 —— 不靠句柄/进程/尺寸。
  2) 平台窗里也有「开始游戏」四个字（详情页/房间页都有按钮），**绝不能绑平台窗**。
  3) 窗口失效 + 正在等某张图 → _checkpoint 能靠画面接回来，不抛 WindowLost。
  4) 画面谁都没有 → 老老实实返回 False，不许瞎绑。
"""
import os
import sys
import threading

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
    """黑底画布；paste=True 时把大厅按钮模板贴进去（= 窗口里画出了大厅）"""
    img = np.zeros((h, w, 3), np.uint8)
    if paste:
        th, tw = TPL.shape[:2]
        y, x = h // 2, max(0, w // 2 - tw // 2)
        img[y:y + th, x:x + tw] = TPL
    return img


class W:
    """GameWindow 的最小替身"""

    def __init__(self, hwnd, size, alive=True):
        self.hwnd = hwnd
        self.pid = 3388
        self.min_size = 400
        self.last_size = size
        self._size = size
        self._alive = alive

    def alive(self):
        return self._alive

    def client_rect(self):
        return (0, 0, self._size[0], self._size[1])

    def fg_same_process(self):
        return True

    def focus(self, *a, **k):
        return True


class Cap:
    """按 hwnd 返回预置画面（真实 Capture 是抓屏，这里换成查表）"""

    def __init__(self, shots):
        self.shots = shots

    def grab(self, win):
        img = self.shots.get(win.hwnd)
        if img is None:
            raise RuntimeError("窗口已失效")
        return img


def make_bot(shots, cur_hwnd, cur_size, alive=True):
    bot = ga.Bot.__new__(ga.Bot)
    bot.cfg_dir = os.getcwd()
    bot.cap = Cap(shots)
    bot.vis = ga.Vision(os.path.join(os.getcwd(), "templates"))
    bot.win = W(cur_hwnd, cur_size, alive=alive)
    bot._cap_ref = [1690, 921]
    bot._wait_spec = None
    bot._rebind_sec = 20.0
    bot._stop = threading.Event()
    bot._paused = threading.Event()
    bot._paused.set()          # set = 可运行（没 set 就永远停在「暂停」里，测试会挂住）
    bot._last_fg = 0.0
    return bot


# 窗口清单：平台窗 1332x945（Qt，里面也画着同一个按钮）、游戏窗 1264x921、记事本 1920x1080
PLAT = {"hwnd": 66058, "pid": 3028, "title": "KK官方对战平台", "cls": "Qt5152QWindowIcon",
        "exe": "Platform.exe", "w": 1332, "h": 945, "offscreen": False, "iconic": False}
GAME = {"hwnd": 197750, "pid": 3388, "title": "Warcraft III", "cls": "Warcraft III",
        "exe": "", "w": 1264, "h": 921, "offscreen": False, "iconic": False}
NOTE = {"hwnd": 555, "pid": 777, "title": "111 - 记事本", "cls": "Notepad",
        "exe": "notepad.exe", "w": 1920, "h": 1080, "offscreen": False, "iconic": False}

real_enum = ga.enum_windows_full
print("① 游戏窗画出大厅 → 绑游戏窗，不绑同样有按钮的平台窗")
try:
    ga.enum_windows_full = lambda: [PLAT, GAME, NOTE]
    shots = {
        PLAT["hwnd"]: canvas(1332, 945, paste=True),   # 平台页里也有「开始游戏」字样
        GAME["hwnd"]: canvas(1264, 921, paste=True),
        NOTE["hwnd"]: canvas(1920, 1080, paste=False),
    }
    bot = make_bot(shots, cur_hwnd=GAME["hwnd"], cur_size=(1264, 921))
    r = bot._bind_by_content({"template": "btn_lobby_start.png", "threshold": 0.85})
    check("按画面内容绑定成功", r, "-> hwnd=%s" % bot.win.hwnd)
    check("绑的是游戏窗，不是平台窗", bot.win.hwnd == GAME["hwnd"], "hwnd=%s" % bot.win.hwnd)
finally:
    ga.enum_windows_full = real_enum

print()
print("② 只有平台窗画着按钮（游戏还在载入）→ 不许绑，返回 False")
try:
    ga.enum_windows_full = lambda: [PLAT, GAME, NOTE]
    shots = {
        PLAT["hwnd"]: canvas(1332, 945, paste=True),
        GAME["hwnd"]: canvas(1264, 921, paste=False),   # 黑屏载入中
        NOTE["hwnd"]: canvas(1920, 1080, paste=False),
    }
    bot = make_bot(shots, cur_hwnd=GAME["hwnd"], cur_size=(1264, 921))
    r = bot._bind_by_content({"template": "btn_lobby_start.png", "threshold": 0.85})
    check("平台窗有画面也不算（排除了 Qt 窗口）", (not r) and bot.win.hwnd == GAME["hwnd"],
          "r=%s hwnd=%s" % (r, bot.win.hwnd))
finally:
    ga.enum_windows_full = real_enum

print()
print("③ 老句柄死了 + 正在等大厅 → _checkpoint 靠画面接回来，不抛 WindowLost")
try:
    # 旧窗口 197750 已失效；新窗口 222333（同尺寸、标题是地图名、类名 TkTopLevel）
    NEW = {"hwnd": 222333, "pid": 3388, "title": "我在峡谷打僵尸 [ by:少女梦 ] 11-15",
           "cls": "TkTopLevel", "exe": "", "w": 1264, "h": 921,
           "offscreen": False, "iconic": False}
    ga.enum_windows_full = lambda: [PLAT, NEW]
    shots = {
        PLAT["hwnd"]: canvas(1332, 945, paste=True),
        NEW["hwnd"]: canvas(1264, 921, paste=True),
    }
    bot = make_bot(shots, cur_hwnd=197750, cur_size=(1264, 921), alive=False)
    bot._wait_spec = {"template": "btn_lobby_start.png", "threshold": 0.85,
                      "any_window": True}
    bot._checkpoint()          # 旧句柄 IsWindow=False → 走按画面找
    check("已切到新窗口", bot.win.hwnd == NEW["hwnd"], "hwnd=%s" % bot.win.hwnd)

    # 反面：没开 any_window（比如别的步骤）→ 仍按老路子 rebind，找不到才抛
    bot2 = make_bot(shots, cur_hwnd=197750, cur_size=(1264, 921), alive=False)
    bot2._wait_spec = {"template": "btn_lobby_start.png", "threshold": 0.85}
    bot2._rebind_sec = 1.0
    raised = False
    try:
        bot2._checkpoint()
    except ga.WindowLost:
        raised = True
    check("没开 any_window 时不走画面绑定（保持原行为）", raised, "raised=%s" % raised)
finally:
    ga.enum_windows_full = real_enum

print()
print("结论：" + ("全部通过 ✅" if ok else "有失败 ❌"))
sys.exit(0 if ok else 1)
