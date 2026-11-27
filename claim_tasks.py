# -*- coding: utf-8 -*-
"""任务页「带红点的分类 → 一键领取 → 确定」批量领奖（用户 2026-11-17 02:12 教的）

用户规则一句话：**任务下方（左侧分类）出现红点的就去点它，然后点「一键领取」，弹窗点「确定」，
把带红点的都领了。**

页面结构（1080 基准，平台主窗 1332×945）：
  左侧分类 5 项，x≈265，块中心 y = 278 / 350 / 421 / 487 / 544
    抽奖券日常 / 抽奖券周常 / 每周探险 / 每周登录 / 每日提升
  分类名右侧（x≈255~300）有个 6~8px 红点表示「该分类有可领」
  右侧页头右上「一键领取」按钮（1080 基准约 939,291，用模板匹配定位更稳）
  点「一键领取」弹浮层「一键领取 / 为您收集了 N 件任务奖励 / 确定」，确定按钮约 (609,504)
  领完后左侧红点消失，tab 上「任务」的红点也一起消失

用法：
  python claim_tasks.py            # 把当前所有带红点的分类都领一遍
  python claim_tasks.py --dry      # 只报哪些分类有红点
  python claim_tasks.py --shot DIR # 过程截图目录（默认 logs/）
"""
from __future__ import annotations

import argparse
import json
import os
import time

import cv2
import numpy as np
import win32gui

import popup_guard as pg
import task_dot as td          # 复用「任务 tab 是否高亮」判据

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_ALL = os.path.join(HERE, "templates", "tpl_claim_all.png")
TPL_OK = os.path.join(HERE, "templates", "tpl_claim_ok.png")
# 「一键领取」纯文字版（2026-11-20 01:03 用户要求）：别的电脑上按钮没有底色，
# 渲染成黑底白字纯文字，按钮模板匹配不上（实测 0.574）。文字模板从用户现场截图裁出，
# 分离度：别的电脑 1.000 / 本机白底按钮样式 0.68~0.72 → 阈值 0.85 稳。
TPL_ALL_TEXT = os.path.join(HERE, "templates", "tpl_claim_all_text.png")
TXT_SCORE = 0.85
# 「确定」纯文字版（2026-11-20 01:12 用户要求做备胎）：从本机浮层截图裁「确定」白字后
# **二值化**（白字 255 / 底色 0），跨底色都能匹配 —— 实测本机蓝底 0.731、黑底白字 1.000，
# 页面负样本仅 0.25 → 阈值 0.70（留足余量）。
TPL_OK_TEXT = os.path.join(HERE, "templates", "tpl_claim_ok_text.png")
OK_TXT_SCORE = 0.70
OK_TXT_REGION = (500, 460, 240, 110)   # 1080 基准：浮层「确定」按钮附近
# 文字模板只在页头右上区域找（1080 基准），避免全图白字误匹配
TXT_REGION = (780, 220, 300, 170)   # x, y, w, h
EVENTS = os.path.join(HERE, "logs", "task_claim_events.jsonl")

BASE_W = 1080.0
CAT_X = 265
CAT_Y = [278, 350, 421, 487, 544]
CAT_ROI = (180, 236, 362, 586)      # 左边界, 上, 右, 下（1080 基准）
TPL_SCORE = 0.80


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


def click1080(hwnd, k, x, y, hold=0.05):
    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    sx, sy = int(round(ox + x / k)), int(round(oy + y / k))
    pg.click_screen(hwnd, sx, sy, hold=hold)
    return sx, sy


def find_red_dots(img):
    """左侧分类区域里的红点 → 返回 [(cx, cy)]（1080 基准）"""
    x0, y0, x1, y1 = CAT_ROI
    roi = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, np.array([0, 120, 120]), np.array([12, 255, 255])) | \
        cv2.inRange(hsv, np.array([168, 120, 120]), np.array([180, 255, 255]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 6 or w < 3 or h < 3 or w > 16 or h > 16:
            continue
        out.append((int(cents[i][0] + x0), int(cents[i][1] + y0)))
    return out


def nearest_cat(y):
    """红点 y → 最近分类块中心 y"""
    return min(CAT_Y, key=lambda v: abs(v - y))


def match_tpl(img, tpl_path, score=TPL_SCORE):
    m = pg.match(img, tpl_path, score)
    if m and m[0] >= score:
        return m
    return None


def wait_tpl(hwnd, tpl, score=TPL_SCORE, timeout=2.6, gap=0.3):
    """轮询等模板稳定出现 → (match, img)。

    平台切分类/弹浮层都有过渡动画，固定 sleep 会抓到半透明中间帧（实测 0.787 vs 0.926）。
    """
    t0 = time.time()
    img = None
    while True:
        img, _ = snap1080(hwnd)
        m = match_tpl(img, tpl, score)
        if m:
            return m, img
        if time.time() - t0 >= timeout:
            return None, img
        time.sleep(gap)


def find_claim_text(img):
    """纯文字版「一键领取」→ (score, (cx,cy), (x,y,w,h)) 或 None（限定页头右上区域）

    同质区域保护（2026-11-20 01:10 实测踩坑）：TM_CCOEFF_NORMED 在纯色区域
    （方差≈0，比如纯黑背景）会因为归一化除零给出虚假的 1.000 分 —— 纯黑图都能
    「命中」。文字模板是黑底白字、本身在高对比区域，所以匹配到的位置像素
    标准差必须 ≥12 才算有效（真文字区域 std 约 60~100，纯色区域 ≈0）。
    """
    x, y, w, h = TXT_REGION
    sub = img[y:y + h, x:x + w]
    # 注意：pg.match 只返回最高分、**不过滤阈值**，必须走 match_tpl（带 >= score 判断），
    # 否则 0.67 的普通白字也会被当成命中（2026-11-20 01:12 实测踩到）
    m = match_tpl(sub, TPL_ALL_TEXT, TXT_SCORE)
    if not m:
        return None
    bx, by, bw, bh = m[2]
    patch = sub[by:by + bh, bx:bx + bw]
    if patch.size == 0 or float(patch.std()) < 12.0:
        return None
    return m[0], (x + m[1][0], y + m[1][1]), m[2]


def wait_claim_all(hwnd, timeout=2.6, gap=0.3):
    """轮询等「一键领取」：先按钮模板（全图），再纯文字模板（页头右上区域）。

    2026-11-20 01:03 用户要求新增文字识别（别的电脑渲染成黑底白字、无按钮底）。
    两者任一命中即返回 (m, img, source)，source = "button" / "text"。
    """
    t0 = time.time()
    img = None
    while True:
        img, _ = snap1080(hwnd)
        if img is not None:
            m = match_tpl(img, TPL_ALL)
            if m:
                return m, img, "button"
            mt = find_claim_text(img)
            if mt:
                return mt, img, "text"
        if time.time() - t0 >= timeout:
            return None, img, None
        time.sleep(gap)


def find_claim_ok_text(img):
    """纯文字版「确定」→ (score, (cx,cy), (x,y,w,h)) 或 None（限定浮层区域 + 同质保护）"""
    x, y, w, h = OK_TXT_REGION
    sub = img[y:y + h, x:x + w]
    m = match_tpl(sub, TPL_OK_TEXT, OK_TXT_SCORE)
    if not m:
        return None
    bx, by, bw, bh = m[2]
    patch = sub[by:by + bh, bx:bx + bw]
    if patch.size == 0 or float(patch.std()) < 12.0:
        return None
    return m[0], (x + m[1][0], y + m[1][1]), m[2]


def wait_claim_ok(hwnd, timeout=2.4, gap=0.3):
    """轮询等「确定」：先按钮模板（全图 0.80），再纯文字模板（浮层区域 0.70）。

    2026-11-20 01:12 用户要求加文字备胎（别的电脑按钮样式可能不同）。
    返回 (m, img, source)，source = "button" / "text" / None。
    """
    t0 = time.time()
    img = None
    while True:
        img, _ = snap1080(hwnd)
        if img is not None:
            m = match_tpl(img, TPL_OK)
            if m:
                return m, img, "button"
            mt = find_claim_ok_text(img)
            if mt:
                return mt, img, "text"
        if time.time() - t0 >= timeout:
            return None, img, None
        time.sleep(gap)


def claim_one(hwnd, k, cat_y, tag, shot):
    """点分类 → 一键领取 → 确定；返回 (ok, n_rewards|None)"""
    click1080(hwnd, k, CAT_X, cat_y)
    # 切分类有过渡动画 → 轮询等「一键领取」稳定出现（实测固定 0.9s 只有 0.787）
    m, img, src = wait_claim_all(hwnd, timeout=2.6)
    save_img(os.path.join(shot, f"claim_{tag}_page.png"), img)
    if not m:
        print(f"   ⚪ {tag}: 这一页没有「一键领取」（按钮/纯文字两种样式都没认出），跳过")
        return False, None
    click1080(hwnd, k, m[1][0], m[1][1])
    print(f"   👆 {tag}: 点「一键领取」{m[1]}（{'按钮' if src == 'button' else '纯文字'}识别）")
    # 浮层同理：等「确定」稳定出现
    m2, img2, src2 = wait_claim_ok(hwnd, timeout=2.4)
    save_img(os.path.join(shot, f"claim_{tag}_dialog.png"), img2)
    if not m2:
        print(f"   ⚠️ {tag}: 没看到「确定」浮层（按钮/纯文字都没认出；"
              f"可能奖励已直接发到账，稍后看红点）")
        return False, None
    click1080(hwnd, k, m2[1][0], m2[1][1])
    print(f"   ✅ {tag}: 点「确定」{m2[1]}（{'按钮' if src2 == 'button' else '纯文字'}识别）")
    time.sleep(1.0)
    return True, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--shot", default=os.path.join(HERE, "logs"))
    ap.add_argument("--max", type=int, default=5, help="最多处理几个分类")
    a = ap.parse_args()

    pg.enable_dpi_awareness()
    hwnd = find_platform()
    if not hwnd:
        print("❌ 没找到平台窗口")
        return 2

    # 页面校验：任务页右侧一定有「一键领取」按钮。找不到说明当前不在
    # 「地图详情 → 任务」页 —— 2026-11-17 22:49 实测 task_dot 崩溃后页面还停在
    # 「排行榜」，本模块把排行榜卡片上的红色当成任务红点（报了 3 个），
    # 若继续就会去点错地方。宁可一个不领，也不在错误页面乱点。
    small0, _k0 = snap1080(hwnd)
    if small0 is None:
        print("❌ 平台截图失败")
        return 3
    # 在不在任务页：以「任务 tab 高亮」为准。旧版只看「一键领取」按钮，
    # 而该按钮在有任务未完成时根本不显示 → 明明在任务页也被拒（2026-11-17 22:56 实测）。
    if (not td.task_tab_active(small0) and not match_tpl(small0, TPL_ALL)
            and not find_claim_text(small0)):
        print("⚠️ 当前不在「任务」页（任务 tab 未选中、也没有一键领取）→ 不执行领券")
        print("   请先点开 地图详情 → 任务 再运行；正常由 daily_claim.py 串联执行")
        return 7

    done = []
    for rnd in range(max(1, a.max)):
        small, k = snap1080(hwnd)
        if small is None:
            print("❌ 截图失败")
            return 3
        dots = find_red_dots(small)
        cands = sorted({nearest_cat(d[1]) for d in dots})
        print(f"🔎 第 {rnd + 1} 轮：左侧红点 {len(dots)} 个 → 分类 y {cands}")
        if not cands:
            break
        if a.dry:
            print("（--dry 只报红点，不点）")
            return 0
        for cy in cands:
            if cy in done:
                continue
            tag = f"y{cy}"
            ok, _ = claim_one(hwnd, k, cy, tag, a.shot)
            if ok:
                done.append(cy)
            time.sleep(0.5)
        if not done:
            print("⚠️ 一轮下来没领到任何分类，停止以免死循环")
            break

    if a.dry:
        return 0

    small2, _ = snap1080(hwnd)
    save_img(os.path.join(a.shot, "claim_after.png"), small2)
    left = find_red_dots(small2) if small2 is not None else []
    print(f"🏁 领完 {len(done)} 个分类：{[f'y{y}' for y in done]}；"
          f"剩余红点 {len(left)} 个" + ("（= 今日任务已全部完成）" if not left else ""))
    try:
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "claimed_cats": done, "left_dots": len(left)},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
