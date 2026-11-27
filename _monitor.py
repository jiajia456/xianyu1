# -*- coding: utf-8 -*-
"""轻量监控：每 0.5 秒看一次窗口，只在状态变化时记一行（不占 CPU、不动鼠标、不置顶）。

记录内容：
  - 平台各窗口（详情页 / 房间 / 弹窗 / 其它尺寸）
  - War3 是否出现、客户区尺寸
  - 当前前台窗口（谁在抢焦点）
Ctrl+C 结束。输出 logs/monitor_时间.log
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import win32gui

import game_auto as ga
import room_finder as rf

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
                   "monitor_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
SIZE_NAME = {(1332, 945): "详情页", (1224, 904): "房间", (584, 517): "建房弹窗"}


def state():
    plat, game = [], []
    for h, t, exe, w, hh in ga.enum_windows():
        e = (exe or "").lower()
        if w <= 100:
            continue
        if e == "platform.exe":
            plat.append(SIZE_NAME.get((w, hh), "%dx%d" % (w, hh)))
        elif e in ("war3.exe", "warcraft iii.exe", "y3.exe"):
            game.append("%dx%d" % (w, hh))
    fg = win32gui.GetWindowText(win32gui.GetForegroundWindow())[:18]
    return sorted(plat), sorted(game), fg


def main():
    last = None
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("# 监控开始 %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        f.flush()
        print("监控输出:", OUT)
        while True:
            plat, game, fg = state()
            cur = (tuple(plat), tuple(game), fg)
            if cur != last:
                line = "%s  平台=[%s]  War3=[%s]  前台=%s" % (
                    time.strftime("%H:%M:%S"),
                    " + ".join(plat) or "无",
                    " + ".join(game) or "无", fg)
                print(line, flush=True)
                f.write(line + "\n")
                f.flush()
                last = cur
            time.sleep(0.5)


try:
    main()
except KeyboardInterrupt:
    print("监控结束")
