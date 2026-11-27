# -*- coding: utf-8 -*-
"""验证新逻辑：点「创建」后只在左上角找「房间号：」，不再整窗轮询截图。"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2

import game_auto as ga
import in_game as ig
import room_finder as rf

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "configs", "wuxia.json"), encoding="utf-8"))

r0 = rf.find_room(verbose=False)
if r0:
    print("先退掉残留房间:", rf.leave_room(r0["hwnd"], verbose=False))
    time.sleep(1.2)

states, stop = [], threading.Event()


def snap():
    out = []
    for h, t, exe, w, hh in ga.enum_windows():
        if (exe or "").lower() == "platform.exe" and w > 100:
            out.append({(1332, 945): "详情页", (1224, 904): "房间", (584, 517): "弹窗"}.get((w, hh), "%dx%d" % (w, hh)))
    return sorted(out)


def monitor():
    last = None
    while not stop.is_set():
        cur = snap()
        if cur != last:
            states.append((time.strftime("%H:%M:%S"), cur))
            last = cur
        time.sleep(0.15)


threading.Thread(target=monitor, daemon=True).start()


def run(name):
    t0 = time.time()
    bot = ga.Bot(CFG, BASE, dry_run=False)
    bot.bind_hotkeys()
    try:
        bot.run_task(name)
    finally:
        bot.input.release_all()
    return time.time() - t0


gw = ga.GameWindow(title_re=["KK官方对战平台"], exe=["Platform.exe"], min_size=400, pick="largest")
ga.Input(gw, True, 0, False, ref_size=ig.REF).click(56, 88)
time.sleep(1.0)

T = {}
T["① 搜图"] = run("search_double")
t0 = time.time(); ga._open_create_dialog(BASE); T["② 弹窗"] = time.time() - t0
T["③a 填密码"] = run("create_room_pwd")
before = ga.room_hwnd_snapshot()
T["③b 提交"] = run("submit_room")
t0 = time.time()
room, waited = ga.await_room_front(before=before)
T["等房间号"] = time.time() - t0
stop.set(); time.sleep(0.3)

print("\n=== 耗时 ===")
for k, v in T.items():
    print("  %-10s %.2fs" % (k, v))
print("\n=== 平台状态 ===")
for ts, st in states:
    print("  ", ts, st)
print("\n房间:", room and "%dx%d score=%.3f hwnd=%s" % (
    room["client"][2], room["client"][3], room.get("score", -1), room["hwnd"]))
if room:
    cv2.imencode(".png", room.get("img") if room.get("img") is not None else
                 rf.grab_client(room["hwnd"], raise_win=False))[1].tofile("_room_new.png")
    print("留在房间，未解散")
