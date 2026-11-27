# -*- coding: utf-8 -*-
"""
「官方检测该账号有作弊风险」弹窗 → 直接结束 War3 进程（然后由上层重开）

背景（02:05 实测）：连续自动跑第 3 轮，游戏内弹：

    官方检测该账号有作弊风险，如果再次出现则永久封禁
    ［重新开始(R)］［降低难度(D)］［装载游戏(L)］［退出战役(Q)］

此时「游戏已经结束」，继续跑四步全是空点，而且停在上面更容易被判。
所以策略改成：**一发现就 taskkill /F 结束 War3，清干净再重开**。

用法：
  python cheat_guard.py --check          # 只检测，命中 exit=0 并打印 hwnd
  python cheat_guard.py --kill           # 命中就结束 War3 进程
  python cheat_guard.py --watch          # 常驻盯着，命中就杀（Ctrl+C 退出）
  python cheat_guard.py --check --shot f.png   # 用已有截图调试

阈值：模板 tpl_cheat_warn.png（510x29 文字条）——实测命中 1.000 / 正常战斗画面 0.28
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time

import cv2
import numpy as np
import win32gui
import win32process
from PIL import ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "templates", "tpl_cheat_warn.png")
SCORE = 0.80          # 命中阈值（实测 1.000 / 干净画面 0.28，余量很大）
WAR3_EXES = ("war3.exe", "warcraft iii.exe", "y3.exe")


def _tpl():
    if not os.path.exists(TPL):
        return None
    return cv2.imdecode(np.fromfile(TPL, dtype=np.uint8), cv2.IMREAD_COLOR)


_TPL = _tpl()


def _client(h):
    class RC(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    rc = RC()
    ctypes.windll.user32.GetClientRect(h, ctypes.byref(rc))
    pt = ctypes.wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(h, ctypes.byref(pt))
    return pt.x, pt.y, rc.r, rc.b


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _pid_exe_name(pid: int) -> str:
    """按 pid 取进程文件名（如 war3.exe）。

    2026-11-18 00:20：成品环境里没有 psutil（装不进沙箱解释器，也没打进包），
    原来唯一的兜底是「窗口标题以 warcraft 开头」，中文标题的 War3 会漏识别。
    这里改成直接调 Win32 API，零第三方依赖，和 room_finder.pid_to_exe 一个路子。
    """
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value or "").lower()
            return ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def war3_windows():
    """所有 War3 顶层窗口（标题 Warcraft III 属于 War3.exe；按进程名兜底）"""
    out = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
            return
        _, pid = win32process.GetWindowThreadProcessId(h)
        title = win32gui.GetWindowText(h)
        if title.lower().startswith("warcraft"):
            out.append((h, pid))
            return
        # 进程名兜底：中文标题 / 玩家改过标题时靠这个认
        if _pid_exe_name(pid) in WAR3_EXES:
            out.append((h, pid))
            return
        # 装了 psutil 的环境再试一次（本机没有，属可选加成）
        try:
            import psutil
            if psutil.Process(pid).name().lower() in WAR3_EXES:
                out.append((h, pid))
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    return out


def grab(h):
    x, y, w, hh = _client(h)
    if w < 600 or hh < 400:
        return None
    try:
        img = np.array(ImageGrab.grab(bbox=(x, y, x + w, y + hh)))
    except Exception:
        return None
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def score(img):
    if img is None or _TPL is None:
        return 0.0
    if img.shape[0] < _TPL.shape[0] or img.shape[1] < _TPL.shape[1]:
        return 0.0
    return float(cv2.matchTemplate(img, _TPL, cv2.TM_CCOEFF_NORMED).max())


def detect(verbose=False):
    """返回 (hwnd, score)；没命中返回 (None, best)"""
    best = 0.0
    for h, pid in war3_windows():
        img = grab(h)
        s = score(img)
        best = max(best, s)
        if verbose:
            print(f"   hwnd={h} pid={pid} 作弊提示得分={s:.3f}", flush=True)
        if s >= SCORE:
            return h, s
    return None, best


def kill_war3(verbose=True):
    """taskkill /F 结束所有 War3 进程；返回剩几个"""
    for exe in ("War3.exe", "Warcraft III.exe"):
        # 不接 text=True：taskkill 的中文输出是 GBK，按 utf-8 解码会抛 UnicodeDecodeError
        subprocess.run(["taskkill", "/F", "/IM", exe, "/T"], capture_output=True)
    time.sleep(1.5)
    left = war3_windows()
    if verbose:
        print(f"   ⛔ 已结束 War3 进程（残留窗口 {len(left)} 个）", flush=True)
    return len(left)


def watch(interval=1.5, on_hit=None):
    """常驻盯：命中就杀。on_hit(hwnd, score) 回调"""
    hits = 0
    while True:
        h, s = detect()
        if h:
            hits += 1
            print(f"[{time.strftime('%H:%M:%S')}] ⚠️ 检测到「作弊风险」提示（score={s:.3f}, hwnd={h}）→ 结束 War3",
                  flush=True)
            kill_war3()
            if on_hit:
                on_hit(h, s)
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--kill", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--shot", default=None, help="用已有截图检测（调试用）")
    ap.add_argument("--interval", type=float, default=1.5)
    args = ap.parse_args()

    if args.shot:
        img = cv2.imdecode(np.fromfile(args.shot, dtype=np.uint8), cv2.IMREAD_COLOR)
        s = score(img)
        print(f"score={s:.3f} → {'命中' if s >= SCORE else '未命中'}")
        return 0 if s >= SCORE else 1

    if args.watch:
        try:
            watch(args.interval)
        except KeyboardInterrupt:
            print("已退出")
        return 0

    h, s = detect(verbose=True)
    if h is None:
        print(f"OK 没有作弊提示（最高分 {s:.3f} < {SCORE}）")
        return 1
    print(f"HIT hwnd={h} score={s:.3f}")
    if args.kill:
        kill_war3()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
