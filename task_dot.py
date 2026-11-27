# -*- coding: utf-8 -*-
"""地图详情页 tab「任务」红点检测 + 点击（用户 2026-11-17 02:08 教的）

规则一句话：**「任务」右上角有红点就点它**，没红点不点。

做法：
  1) 抓平台主窗，缩到宽 1080（跟肉眼定位的截图同尺度）
  2) 模板匹配 templates/tpl_tab_task.png 找「任务」tab（地图详情页 tab 行的第 6 个）
  3) 在「任务」文字右上角的小方框里统计红色像素（HSV 双区间 0~12 / 168~180）
     ≥ 8 个像素就判「有红点」
  4) 有红点 → 点「任务」文字中心；没红点 → 什么都不做

用法：
  python task_dot.py             # 检测，有红点就点一下（默认）
  python task_dot.py --dry       # 只检测不点击
  python task_dot.py --keep      # 点完不截图（默认会存 logs/task_after.png）
"""
from __future__ import annotations

import argparse
import os
import time

import cv2
import numpy as np
import win32gui

import popup_guard as pg

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_TASK = os.path.join(HERE, "templates", "tpl_tab_task.png")
TPL_ALL = os.path.join(HERE, "templates", "tpl_claim_all.png")   # 任务页特征：一键领取

# 「任务」tab 文字区域（1080 基准），右边界 586 故意避开右侧红点
TAB_ROI = (568, 190, 586, 209)
TAB_BLUE_MIN = 0.08          # 选中态文字变蓝，实测 0.23；未选中 0.00
BASE_W = 1080.0
TPL_SCORE = 0.80
RED_MIN = 8


def save_img(path, img):
    """存证截图。任何失败都不能影响主流程（本环境覆盖既有文件会被拒）。"""
    if img is None:
        return None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except Exception:
        pass
    root, ext = os.path.splitext(path)
    cands = [path, "%s_%s%s" % (root, time.strftime("%H%M%S"), ext or ".png")]
    for p in cands:
        try:
            cv2.imencode(".png", img)[1].tofile(p)
            return p
        except Exception:
            continue
    return None


def task_tab_active(img):
    """「任务」tab 是否处于选中态（文字变蓝）。这是最可靠的「在任务页」判据。"""
    if img is None:
        return False
    x0, y0, x1, y1 = TAB_ROI
    roi = img[y0:y1, x0:x1]
    if roi.size == 0:
        return False
    b = roi[:, :, 0].astype(int)
    g = roi[:, :, 1].astype(int)
    r = roi[:, :, 2].astype(int)
    blue = ((b > 120) & (b > r + 40) & (b > g + 15)).mean()
    return blue >= TAB_BLUE_MIN


def in_task_page(img):
    """验证是否真的进了任务页。

    主判据 = tab 高亮（稳定）；「一键领取」按钮只作补充 —— 今天所有任务都是
    「未完成」时该按钮不显示，只靠它会误判。
    """
    if task_tab_active(img):
        return True
    if img is None:
        return False
    m = pg.match(img, TPL_ALL, 0.80)
    return bool(m and m[0] >= 0.80)


def find_platform():
    cands = pg.enum_candidates(False)
    return cands[0][0] if cands else None


def snap1080(hwnd):
    img = pg.grab_hwnd(hwnd)
    if img is None:
        return None, 1.0
    k = BASE_W / img.shape[1]
    return cv2.resize(img, (int(BASE_W), int(round(img.shape[0] * k))),
                      interpolation=cv2.INTER_AREA), k


def red_count(img, x0, y0, x1, y1):
    """统计区域内红色像素个数（红点/红徽章都算）"""
    y0, y1 = max(0, y0), min(img.shape[0], y1)
    x0, x1 = max(0, x0), min(img.shape[1], x1)
    roi = img[y0:y1, x0:x1]
    if roi.size == 0:
        return 0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, np.array([0, 120, 120]), np.array([12, 255, 255])) | \
        cv2.inRange(hsv, np.array([168, 120, 120]), np.array([180, 255, 255]))
    return int(m.sum() // 255)


def detect(img):
    """返回 (has_dot, center, score)；center 是 1080 基准的「任务」文字中心"""
    m = pg.match(img, TPL_TASK, TPL_SCORE)
    if not m or m[0] < TPL_SCORE:
        return None, None, (m[0] if m else 0.0)
    score, (cx, cy), (bx, by, bw, bh) = m
    # 红点在文字右上角：从文字右边界往右 15px、上边界往上 5px 开一个 16x16 的窗
    n = red_count(img, bx + bw - 2, by - 5, bx + bw + 14, by + bh - 4)
    return n >= RED_MIN, (cx, cy), score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只检测不点击")
    ap.add_argument("--at", type=int, default=0, help="手动指定点击的基准 x（标定用）")
    ap.add_argument("--keep", action="store_true", help="点完不截图")
    a = ap.parse_args()

    pg.enable_dpi_awareness()

    # 2026-11-18 13:56 用户指出：打完一局「退出游戏」后屏幕停在**游戏房间**窗口，
    # 里面没有顶部「签到」和 tab「任务」→ 先带回「地图详情」页再往下走
    try:
        import plat_home as ph
        _r = ph.ensure_detail(verbose=True)
        if not _r.get("ok"):
            print(f"   \u26a0\ufe0f 平台没回到「地图详情」页（{_r.get('reason')}）——后面可能找不到按钮")
    except Exception as _e:
        print(f"   (平台形态校正跳过：{_e!r})")
    hwnd = find_platform()
    if not hwnd:
        print("❌ 没找到平台窗口")
        return 2

    small, k = snap1080(hwnd)
    if small is None:
        print("❌ 平台截图失败")
        return 3
    save_img(os.path.join(HERE, "logs", "task_before.png"), small)

    has_dot, center, score = detect(small)
    if center is None:
        print(f"❌ 没找到「任务」tab（模板 score={score:.3f}）——当前页面可能不是地图详情页")
        return 4

    print(f"🔎 「任务」tab 1080 基准{center} 模板={score:.3f} "
          f"红点={'有' if has_dot else '没有'}")
    if has_dot is False:
        print("✅ 任务无红点 → 判定今日任务已完成，跳过（不点）")
        return 0
    if a.at:
        center = (a.at, center[1])
        print(f"（--at 指定基准 x={a.at}，不用模板中心）")

    if a.dry:
        print("（--dry 只检测不点击）")
        return 0

    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    sx, sy = int(round(ox + center[0] / k)), int(round(oy + center[1] / k))
    print(f"👆 点击「任务」→ 屏幕({sx},{sy})")
    pg.click_screen(hwnd, sx, sy)
    time.sleep(1.2)
    # 点完必须确认真的进了任务页 —— 2026-11-17 22:50 实测模板右边缘带红点，
    # 框中心正好落在红点上，点下去切到了「社区攻略」，旧代码却直接报成功。
    entered = False
    tried = []
    for dx in (0, -10, -20, -30, 10):
        if dx:
            cx2 = center[0] + dx
            sx2, sy2 = int(round(ox + cx2 / k)), int(round(oy + center[1] / k))
            pg.click_screen(hwnd, sx2, sy2)
            time.sleep(1.1)
        chk, _ = snap1080(hwnd)
        tried.append((center[0] + dx, in_task_page(chk)))
        if in_task_page(chk):
            entered = True
            if dx:
                print(f"🔧 原坐标没点中，横向微调 {dx:+d} 后进入任务页（基准 x={center[0] + dx}）")
            break
        print(f"   ⚠️ 基准 x={center[0] + dx} 点下去没进任务页，换个位置再试")
    if not entered:
        print(f"❌ 「任务」tab 点了 {len(tried)} 个位置仍未进入任务页：{tried}")
        print("   请手动点开「任务」，或用 e → python task_dot.py --at <基准x> 标定坐标")
        return 5

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
