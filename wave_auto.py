# -*- coding: utf-8 -*-
"""
主线挑战「自动挑战」开关 —— 右键图标 + 成功后校验 + 失败重试

用户 2026-11-16 00:39 教：
  右键单击左侧「第X波 主线挑战(C)」图标 = 开关自动挑战
  鼠标移开后，图标上出现「第X波(自动)」（同时顶部有红字「挑战中」）= 开启成功
  没有出现「自动」= 没生效，需要重新右键单击

判据（实测）：
  图标区域 (262,596)-(336,674) 内「挑战中」红字像素
    未开启（显示「已通关」/无波次）: 0 个
    已开启（显示「挑战中 / 第7波(自动)」）: ~173 个（占比 1.5%）
  阈值 RED_MIN=25，非常干净

用法：
  python wave_auto.py                 # 检查，没开就右键，失败重试，最多 5 次
  python wave_auto.py --retries 8     # 最多试 8 次
  python wave_auto.py --check-only    # 只检查不点击（看当前是否已开启）
  python wave_auto.py --click 1       # 只右键 1 次，不做校验重试
"""
from __future__ import annotations

import argparse
import time

import cv2
import numpy as np
import win32api
import win32con
import win32gui
from PIL import ImageGrab

import game_auto as ga

# ---- 坐标（客户区 1690x921）----
ICON = (293, 632)                     # 城堡图标中心
REGION = (262, 596, 336, 674)         # (x0, y0, x1, y1) 图标圆内
RED_MIN = 25                          # 红字像素阈值
CLEAR_YELLOW_MIN = 150                # 「已通关」黄字像素阈值（实测 225，空画面 0）
PARK = (900, 300)                     # 截图/判断前把鼠标停到这，避免 tooltip 遮挡


def grab(w) -> np.ndarray:
    cx, cy, cw, ch = w.client_rect()
    img = np.array(ImageGrab.grab(bbox=(cx, cy, cx + cw, cy + ch)))
    out = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    # 与 in_game.grab 保持一致：统一缩放回 1690x921 基准，坐标常量才不会错位
    try:
        import in_game as _ig
        return _ig.norm(out)
    except Exception:
        return out


def red_pixels(img, region=REGION) -> int:
    x0, y0, x1, y1 = region
    roi = img[y0:y1, x0:x1]
    b = roi[:, :, 0].astype(int)
    g = roi[:, :, 1].astype(int)
    r = roi[:, :, 2].astype(int)
    return int(((r > 140) & (g < 80) & (b < 80)).sum())


def yellow_pixels(img, region=REGION) -> int:
    """图标圆内的黄字像素 —— 主线打满时显示「已通关」，是明亮的金黄色。"""
    x0, y0, x1, y1 = region
    roi = img[y0:y1, x0:x1]
    b = roi[:, :, 0].astype(int)
    g = roi[:, :, 1].astype(int)
    r = roi[:, :, 2].astype(int)
    return int(((r > 180) & (g > 130) & (b < 100)).sum())


def is_auto_on(img) -> tuple[bool, int]:
    n = red_pixels(img)
    return n >= RED_MIN, n


def auto_state(img) -> tuple[str, dict]:
    """图标圆内的三态判据（2026-11-18 09:41 用户指出「已通关」的含义后新增）。

    返回 ("on"/"cleared"/"off"/"blank", {"red":int,"yellow":int})

      on      : 红字「挑战中」在（实测 173）→ 自动挑战开着
      cleared : 红字 0 但黄字「已通关」在（实测 225）→ **主线挑战任务已完成**，
                不是自动掉了，绝对不能再右键补刀（右键是 toggle，会真关掉）
      blank   : 红字黄字都 0 → 大厅/菜单/黑屏，不在游戏内，也什么都不做
      off     : 其它（红字 0、黄字零星）→ 才可能是真的掉线
    """
    n_red = red_pixels(img)
    n_yel = yellow_pixels(img)
    if n_red >= RED_MIN:
        st = "on"
    elif n_yel >= CLEAR_YELLOW_MIN:
        st = "cleared"
    elif n_red == 0 and n_yel == 0:
        st = "blank"
    else:
        st = "off"
    return st, {"red": n_red, "yellow": n_yel}


def park(w, inp=None, pos=PARK):
    cx, cy, _, _ = w.client_rect()
    win32api.SetCursorPos((cx + pos[0], cy + pos[1]))


def bind():
    """绑定 War3 窗口；窗口被最小化时先恢复（此时 GetWindowRect 是 -32000，引擎会认不出）。"""
    def try_bind():
        try:
            return ga.GameWindow(title_re=["Warcraft"], exe=["War3.exe"], min_size=400)
        except Exception:
            return None

    w = try_bind()
    if w is None:
        # 枚举所有 Warcraft 窗口，把最小化的恢复
        hwnds = []

        def cb(h, _):
            if win32gui.GetWindowText(h).lower().startswith("warcraft"):
                hwnds.append(h)

        win32gui.EnumWindows(cb, None)
        for h in hwnds:
            if win32gui.IsIconic(h):
                win32gui.ShowWindow(h, win32con.SW_RESTORE)
                time.sleep(1.0)
        for _ in range(10):
            w = try_bind()
            if w is not None:
                break
            time.sleep(0.8)
    if w is None:
        return None
    if win32gui.IsIconic(w.hwnd):
        win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
        time.sleep(1.0)
    flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
    win32gui.SetWindowPos(w.hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
    return w


def _save_png(path, img):
    """中文路径安全的存图（cv2.imwrite 在中文路径下静默失败）"""
    try:
        import cv2
        ok, buf = cv2.imencode(".png", img)
        if ok:
            buf.tofile(path)
            return True
    except Exception:
        pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retries", type=int, default=5, help="最多右键几次")
    ap.add_argument("--check-only", action="store_true", help="只检查不点击")
    ap.add_argument("--click", type=int, default=0, help="固定右键 N 次，不做校验")
    ap.add_argument("--wait", type=float, default=1.2, help="右键后等待秒数")
    args = ap.parse_args()

    w = bind()
    if w is None:
        print("❌ 没找到 War3 窗口（游戏没运行或被最小化到无法恢复）")
        return 2

    inp = ga.Input(w, True, 0, False, ref_size=(1690, 921))
    w.focus()
    time.sleep(0.4)
    park(w)
    time.sleep(0.9)

    on, n = is_auto_on(grab(w))
    print(f"当前状态：{'已开启(自动)' if on else '未开启'}  红字像素={n}（阈值 {RED_MIN}）")

    if args.check_only:
        return 0 if on else 1
    if on:
        print("✅ 自动挑战本来就在跑，不需要右键")
        return 0

    if args.click:
        for i in range(args.click):
            park(w)
            time.sleep(0.2)
            inp.click(ICON[0], ICON[1], "right", 0.05, 1)
            print(f"第 {i+1} 次右键单击 @{ICON}")
            time.sleep(args.wait)
        park(w)
        time.sleep(0.8)
        on, n = is_auto_on(grab(w))
        print(f"点完 {args.click} 次后：{'已开启(自动)' if on else '仍未开启'} 红字像素={n}")
        return 0 if on else 1

    for i in range(1, args.retries + 1):
        w.focus()
        time.sleep(0.25)
        park(w)
        time.sleep(0.2)
        inp.click(ICON[0], ICON[1], "right", 0.05, 1)
        park(w)                      # 立刻移开鼠标，避免 tooltip 挡住判断区域
        time.sleep(args.wait)
        img = grab(w)
        on, n = is_auto_on(img)
        print(f"第 {i} 次右键 → 红字像素={n} → {'✅ 出现「自动」，成功' if on else '❌ 没出现「自动」'}")
        if on:
            _save_png("shot_wave_auto_on.png", img)
            print("✅ 自动挑战已开启")
            return 0
        time.sleep(0.5)

    _save_png("shot_wave_auto_fail.png", grab(w))
    print(f"⚠️ 试了 {args.retries} 次都没出现「自动」。可能原因：")
    print("   · 本波已通关（图标显示「已通关」/ 上方提示「暂无更多波次」）→ 没得挑战")
    print("   · 鼠标点偏了（图标位置变了）")
    print("   → 已存 shot_wave_auto_fail.png，看一眼再决定")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
