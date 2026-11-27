# -*- coding: utf-8 -*-
"""
「游戏已经结束」守卫 —— 画面上出现「返回平台」按钮时，点它回平台，交给主循环重开游戏。

用户 2026-11-16 23:03 需求：
  「出现这个（游戏已经结束… + 返回平台）→ 选择返回平台，重开游戏继续」

现象：一局被提前结束（角色死亡/掉线/被踢）后，游戏画面变黑，左上角橙字「游戏已经结束…」，
屏幕上方中间一个灰蓝按钮「返回平台」。画面里什么都点不动，只能点这个按钮回平台，
然后由 full_loop 的 A 阶段重新开局。

用法：
  python end_guard.py --check              # 只检测当前画面有没有这个状态
  python end_guard.py --click              # 有就点「返回平台」
  python end_guard.py --check logs/x.png   # 用截图调试模板
被 in_game.grab_checked() 自动调用：命中 → 点掉 → 抛 RuntimeError(带 MARK)，
各阶段脚本收到异常就退出，full_loop 看到输出里的 MARK 会把本轮判为「可恢复中断」，
杀进程 → 等一会儿 → 重跑同一轮。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import cv2
import numpy as np
import win32api
import win32con
import win32gui
from ctypes import windll

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_BACK = os.path.join(HERE, "templates", "btn_back_platform.png")
TPL_OVER = os.path.join(HERE, "templates", "lbl_game_over.png")
SCORE = 0.84                  # 返回平台按钮：实测 1.000，不在场 ≤0.45
SCORE_OVER = 0.88             # 「游戏已经结束…」橙字
MARK = "[BACK_PLATFORM] 游戏已结束 —— 已点「返回平台」回平台，准备重开"


def _load(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def find(img, tpl_path, score):
    """返回 (score, (cx, cy)) 或 None"""
    if img is None or not img.size:
        return None
    tpl = _load(tpl_path)
    if tpl is None or tpl.shape[0] > img.shape[0] or tpl.shape[1] > img.shape[1]:
        return None
    res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    h, w = tpl.shape[:2]
    return mx, (loc[0] + w // 2, loc[1] + h // 2)


def detect(img):
    """返回 ('back', score, (cx,cy)) / ('over', score, (cx,cy)) / None"""
    m = find(img, TPL_BACK, SCORE)
    if m and m[0] >= SCORE:
        return "back", m[0], m[1]
    m2 = find(img, TPL_OVER, SCORE_OVER)
    if m2 and m2[0] >= SCORE_OVER:
        return "over", m2[0], m2[1]
    return None


def _restore(hwnd, tries=12):
    """窗口最小化时 ClientToScreen 会返回 -32760 这种垃圾值 → 必须先还原并等它回来"""
    for _ in range(tries):
        ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
        if ox > -10000 and oy > -10000:
            return ox, oy
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        time.sleep(0.25)
    return win32gui.ClientToScreen(hwnd, (0, 0))


def click_client(hwnd, x, y, hold=0.06):
    """在窗口客户区坐标点一下。

    实测有两种情况：
      1) 正常窗口 → 算屏幕坐标 + 真鼠标事件
      2) 平台把游戏窗挪到屏幕外（GetWindowRect 全是 -32768）→ 屏幕点击无效，
         直接 PostMessage 鼠标消息（Qt 窗口认这个，实测点「返回平台」能生效）
    """
    try:
        r = win32gui.GetWindowRect(hwnd)
    except Exception:
        return False
    if r[0] < -10000 or r[1] < -10000:
        lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        try:
            win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)
            time.sleep(0.12)
            win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
            time.sleep(0.1)
            win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)
            return True
        except Exception as e:
            print(f"      (窗口在屏幕外，PostMessage 也失败：{e!r})", flush=True)
            return False
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        win32gui.BringWindowToTop(hwnd)
    except Exception:
        pass
    ox, oy = _restore(hwnd)          # ← 关键：等窗口真的回到屏上再取原点
    if ox < -10000 or oy < -10000:
        print("      (窗口还处于最小化，点不了)", flush=True)
        return False
    time.sleep(0.15)
    win32api.SetCursorPos((int(ox + x), int(oy + y)))
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(hold)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def enum_shell():
    """枚举可能承载「游戏已结束」画面的窗口。

    实测：游戏一结束，War3 窗口标题会变成平台壳的 `wyplatform_war3_game`（1690x922），
    连进程都不再是 War3.exe，所以既不能按标题绑也不能按 exe 绑，只能按标题前缀找。
    """
    out = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        t = win32gui.GetWindowText(h) or ""
        low = t.lower()
        r = win32gui.GetClientRect(h)
        if r[2] < 800 or r[3] < 600:
            return
        if low.startswith("wyplatform") or low.startswith("warcraft"):
            out.append((h, t, (r[2], r[3])))

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda x: -x[2][0] * x[2][1])
    return out


def recover(verbose=True):
    """按标题前缀找窗口 → 检测「游戏已结束」→ 点「返回平台」。命中返回 True。"""
    import popup_guard as pg
    for hwnd, title, size in enum_shell():
        img = pg.grab_hwnd(hwnd)
        hit = detect(img)
        if not hit:
            continue
        if verbose:
            print(f"   ⛔ 命中「游戏已结束」窗口 {title!r} {size[0]}x{size[1]} "
                  f"({hit[0]} score={hit[1]:.3f}) → 点「返回平台」", flush=True)
        try:
            click_client(hwnd, hit[2][0], hit[2][1])
        except Exception as e:
            if verbose:
                print(f"      (点击失败：{e!r})", flush=True)
        # 兜底：点完「返回平台」守候几秒（主力是常驻监控 popup_watch.py）
        try:
            k = pg.sweep(6.0, quiet=True)
            if k and verbose:
                print(f"   🧹 返回平台后守候期间清掉 {k} 个弹窗", flush=True)
        except Exception:
            pass
        return True
    return False


def guard(img, win=None, click=True, verbose=True):
    """命中「游戏已结束」就点「返回平台」。返回 True 表示命中了。

    win 传 game_auto.GameWindow（用它的 hwnd）；不传就只在检测层面返回 True 不点。
    """
    hit = detect(img)
    if not hit:
        return False
    kind, score, (cx, cy) = hit
    if kind == "over" and win is None:
        if verbose:
            print(f"   ⛔ 检测到「游戏已经结束」（score={score:.3f}）", flush=True)
        return True
    if kind == "over":
        # 只看到橙字、没看到按钮：等一下再抓（按钮可能还没画出来）
        time.sleep(0.6)
        return True
    if verbose:
        print(f"   ⛔ 检测到「返回平台」按钮（score={score:.3f} @{cx},{cy}）→ 点击回平台", flush=True)
    if click and win is not None:
        try:
            # img 来自 in_game.grab()，已归一化到 1690x921；点击前要换算回实际客户区
            # （否则小窗口下会把鼠标点到别处 —— 2026-11-17 换电脑后统一处理）
            tx, ty = cx, cy
            try:
                _, _, cw, ch = win.client_rect()
                if cw > 0 and ch > 0:
                    tx = int(round(cx * cw / 1690.0))
                    ty = int(round(cy * ch / 921.0))
            except Exception:
                pass
            click_client(win.hwnd, tx, ty)
        except Exception as e:
            if verbose:
                print(f"      (点「返回平台」失败：{e!r})", flush=True)
        # 兜底：同上
        try:
            import popup_guard as _pg
            k = _pg.sweep(6.0, quiet=True)
            if k and verbose:
                print(f"   🧹 返回平台后守候期间清掉 {k} 个弹窗", flush=True)
        except Exception:
            pass
    return True


def main():
    ap = argparse.ArgumentParser(description="「游戏已经结束 → 返回平台」守卫")
    ap.add_argument("--check", nargs="?", const="__live__", default=None,
                    help="只检测：不带参数=当前窗口，带路径=用截图")
    ap.add_argument("--click", action="store_true", help="命中就点「返回平台」")
    ap.add_argument("--window-title", default="Warcraft")
    args = ap.parse_args()

    try:
        windll.shcore.SetProcessDpiAwareness(2)     # Win8.1+ per-monitor
    except Exception:
        try:
            windll.user32.SetProcessDPIAware()      # Win7 / 老系统 system-aware
        except Exception:
            pass

    if args.check and args.check != "__live__":
        img = _load(args.check)
        hit = detect(img)
        print(hit or "没检测到「游戏已结束」画面", flush=True)
        return 0 if hit else 1

    import game_auto as ga
    try:
        win = ga.GameWindow(exe=["War3.exe", "Warcraft III.exe"])
        img = ga.Capture("auto").grab(win)
    except Exception as e:
        print(f"(按 exe 绑窗口失败：{e}) → 走兜底枚举", flush=True)
        ok = recover()
        print("已点「返回平台」" if ok else "兜底也没找到「游戏已结束」窗口", flush=True)
        return 0 if ok else 1
    hit = detect(img)
    if not hit:
        print("当前画面正常（没有「游戏已结束」）", flush=True)
        return 1
    print(f"命中：{hit[0]} score={hit[1]:.3f} @{hit[2]}", flush=True)
    if args.click:
        guard(img, win, click=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
