# -*- coding: utf-8 -*-
"""
「提前挑战」监控器 —— 每分钟扫一次画面，出现图标就双击并确认「是」

用户 2026-11-16 12:13 需求：
  监控游戏，频率 1 分钟一次；画面里出现「提前挑战」图标（紫色水晶 + 黄字）就继续操作：
  双击图标 → 弹出「是否开启提前挑战?」对话框 → 点「是」。

判据（实测分离度）：
  图标 btn_early_challenge.png (79x51)：在场 1.000 / 不在场 ≤0.53
  另加颜色闸门（防小模板误报）：匹配框内 黄字像素≥120、紫色像素≥90
     真实图标：黄 191 / 紫 138；随机区域：黄 0~492、紫 0~58（同时满足的极少）
  弹窗 dlg_early_title.png (400x30)：「是否开启提前挑战?」标题条
     在场 1.000 / 不在场 0.416 → 阈值 0.85
  是按钮 btn_popup_yes.png (116x38)：在场 1.000 / 不在场 0.526 → 阈值 0.90
     兜底：按标题命中位置偏移 (+104, +159) 得按钮中心

流程（每轮，优先级从上到下）：
  0) 「是否开启提前挑战?」弹窗已经开着 → 直接点「是」（避免上一轮没点掉/重复双击）
  1) 见到「挑战存档」按钮（绿框白字 157x50）→ 点它 = **本轮通关**
  2) 见到「提前挑战」图标 → 双击 → 等弹窗最多 6s → 点「是」→ 复查弹窗消失（最多 3 次）
  3) 都没有 → 记一行「未见」，等下一个 60s

「游戏通关」的标准流程（2026-11-16 12:31 用户定义）= 四步串起来：
  ① 游戏中准备四步  : in_game.py（技能 3 / 装备 4 / 升级武器 3 / 自动挑战）
  ② 提前挑战        : 双击紫色水晶图标 → 弹窗点「是」
  ③ 挑战存档        : 点绿框白字「挑战存档」→ 弹出「存档挑战」面板
  ④ 存档挑战        : 面板右侧 12 格（4x3）每格双击一次、间隔 1s、整轮 2 遍
                      坐标见 save_challenge.py（第1格 1371,733，步长 76）
  一键入口：start_clear.bat（先四步，跑通后自动进监控，③ 成功即自动接 ④）

安全：
  截图走 in_game.grab_checked()，出现「作弊风险」提示会直接结束 War3 并停止监控。

用法：
  python watch_early.py                    # 60s 一次，无限循环
  python watch_early.py --interval 60      # 60s 一次（full_loop 默认传 60）
  python watch_early.py --once             # 只扫一轮（调试）
  python watch_early.py --times 10         # 扫 10 轮后退出
  python watch_early.py --dry              # 只识别不点击
  python watch_early.py --shot             # 每轮存一张截图到 logs/early_*.png
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import cv2
import numpy as np

import game_auto as ga
import in_game as ig
import wave_auto as wv

try:
    import login_guard as lg          # 2026-11-19 20:00：④ 段期间登录守卫搬到本进程跑
    _LG_OK = True
except Exception as _e:
    _LG_OK = False

TPL_ICON = "templates/btn_early_challenge.png"
TPL_DLG = "templates/dlg_early_title.png"
TPL_YES = "templates/btn_popup_yes.png"
TPL_SAVE = "templates/btn_challenge_save.png"     # 「挑战存档」（绿框白字 157x50）

SCORE_ICON = 0.78
SCORE_DLG = 0.85
SCORE_YES = 0.90
SCORE_SAVE = 0.85
YELLOW_MIN = 120
PURPLE_MIN = 90
OFF_YES = (104, 159)      # 是按钮中心 - 标题命中点
PARK = (1200, 190)        # 判断/等待时鼠标停这，避免 tooltip 遮挡

LOG_PATH = time.strftime("logs/early_watch_%Y%m%d_%H%M.log")
_LINES = []


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _LINES.append(line)
    try:
        os.makedirs("logs", exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load(p: str) -> np.ndarray:
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def match(img, tpl):
    if img is None or tpl is None or img.shape[0] < tpl.shape[0] or img.shape[1] < tpl.shape[1]:
        return -1.0, (0, 0)
    r = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, ml = cv2.minMaxLoc(r)
    return float(mx), ml


def color_ok(img, x, y, w, h) -> tuple[bool, int, int]:
    roi = img[y:y + h, x:x + w]
    if roi.size == 0:
        return False, 0, 0
    b = roi[:, :, 0].astype(int)
    g = roi[:, :, 1].astype(int)
    r = roi[:, :, 2].astype(int)
    yellow = int(((r > 170) & (g > 120) & (b < 110)).sum())
    purple = int(((r > 110) & (b > 120) & (g < 110)).sum())
    return (yellow >= YELLOW_MIN and purple >= PURPLE_MIN), yellow, purple


def find_popup(img):
    s, p = match(img, TPL_DLG)
    return (s >= SCORE_DLG), s, p


def find_save(img):
    s, p = match(img, TPL_SAVE)
    return (s >= SCORE_SAVE), s, p


def find_icon(img):
    s, p = match(img, TPL_ICON)
    if s < SCORE_ICON:
        return False, s, p, (0, 0)
    ok, yellow, purple = color_ok(img, p[0], p[1], TPL_ICON_W, TPL_ICON_H)
    return ok, s, p, (yellow, purple)


def click_yes(w, inp, img, dry: bool) -> bool:
    """弹窗已开：优先按模板找「是」，找不到就用标题位置偏移兜底。"""
    s, p = match(img, TPL_YES)
    if s >= SCORE_YES:
        cx, cy = p[0] + TPL_YES_W // 2, p[1] + TPL_YES_H // 2
        how = f"模板 {s:.3f}"
    else:
        ok, ds, dp = find_popup(img)
        if not ok:
            return False
        cx, cy = dp[0] + OFF_YES[0], dp[1] + OFF_YES[1]
        how = f"偏移（标题 {ds:.3f}）"
    if dry:
        log(f"   [dry] 双击「是」({cx},{cy}) 来源 {how}")
        return True
    # 用户 2026-11-18 19:57：单击改双击
    inp.click(cx, cy, hold=0.06, clicks=2, gap=0.12)
    log(f"   ✅ 已双击「是」({cx},{cy}) 来源 {how}")
    return True


def wait_popup_gone(w, timeout=3.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.4)
        ok, _, _ = find_popup(ig.grab(w))
        if not ok:
            return True
    return False


# ---------------- 自动挑战守护（2026-11-18 00:47 新增）----------------
# 用户反馈：「这个（第N波 主线挑战）不出现自动，就无法出现提前挑战关联了」。
# 实测确认：右键开启后图标圆内出现红字「挑战中」（红字像素 173），主线挑战波次会推进
# （第1波 → 第3波）；自动一掉红字回到 0，提前挑战就再不出现 → C 阶段白等 800 秒。
# 原实现只在 in_game 第③步查一次，这一局就是这么挂的，所以这里每轮再守一次。
AUTO_GUARD_TRIES = 2       # 每轮最多补刀几次
AUTO_GUARD_CONFIRM = 3     # 连续几轮读到红字 0 才判定「真的掉了」
AUTO_GUARD_COOLDOWN = 6    # 补刀失败后静默多少轮再考虑重试
AUTO_GUARD_MAX_ROUNDS = 3  # 整局最多发起几轮补刀，超过就彻底停手
# 2026-11-18 02:15 修正判据：波与波之间有十几秒空档，此时「挑战中」红字本来就会
# 消失（实测本局红字 0 出现 6 次，补刀 6 次全失败，主线照样打满 50 波）。
# 所以不能再「一读到 0 就右键」—— 右键是 toggle，乱点有把开着的关掉的风险。
# 现在的规则：连续 3 轮（≈90s）都是 0 才算真的掉了；补刀失败后静默 6 轮；
# 整局最多发起 3 轮补刀，之后彻底不碰。
_auto_zero_streak = 0      # 连续读到红字 0 的轮数
_auto_cooldown = 0         # 还要静默几轮
_auto_rounds = 0           # 已发起过几轮补刀
_auto_gave_up = False      # 彻底停手


def ensure_auto(w, inp, args) -> str:
    """未开启就右键补刀；已开启什么都不做。

    2026-11-18 00:52 实测补了两点，否则会「越补越坏」：
      1) 右键是「开关」语义 —— 盲点会把已经开着的自动关掉，所以必须先查再点，
         而且点完要等够（2.2s）再判，避免「其实开了但没读到 → 又点一下关回去」；
      2) War3 对「鼠标刚移到就按下」经常不响应（game_auto.click_hover 的注释也
         提过这毛病）→ 补刀改用 click_hover，先停 0.5s 让鼠标进入事件生效。
    """
    # 窗口被最小化时截图/点击全废（这局 C 阶段就是这么中断的），先恢复
    try:
        import win32con
        import win32gui
        if win32gui.IsIconic(w.hwnd):
            win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
            time.sleep(0.9)
            log("   ↳ 窗口是最小化状态 → 已恢复")
    except Exception:
        pass

    # 三态判据（2026-11-18 09:41 用户指出「已通关」= 主线任务已完成）：
    # 主线打满后图标圆内显示黄字「已通关」，红字「挑战中」本来就没有了 ——
    # 旧逻辑只看红字，会把这当成「自动挑战掉了」，每局白白右键补刀 6~8 次。
    st, px = wv.auto_state(ig.grab(w))
    if st == "on":
        return "on"
    if st == "cleared":
        global _auto_gave_up
        _auto_gave_up = True
        log(f"   ✔ 主线挑战已通关（黄字 {px['yellow']}）→ 自动挑战无需补刀")
        return "cleared"
    if st == "blank":
        log("   （图标区空 → 不在游戏内，跳过补刀）")
        return "blank"
    n = px["red"]
    log(f"⚠️ 自动挑战掉了（红字 {n}／黄字 {px['yellow']}）→ 右键补刀（提前挑战依赖它开着）")
    for i in range(1, AUTO_GUARD_TRIES + 1):
        if args.dry:
            log("   [dry] 不点击")
            return "dry"
        w.focus()
        time.sleep(0.35)
        wv.park(w, inp)
        time.sleep(0.2)
        inp.click_hover(wv.ICON[0], wv.ICON[1], hover=0.5, hold=0.06,
                        button="right", clicks=1)
        wv.park(w, inp)
        time.sleep(2.2)
        on, n = wv.is_auto_on(ig.grab(w))
        log(f"   补刀第 {i} 次 → 红字={n} → {'✅ 已开启' if on else '❌ 没出现'}")
        if on:
            return "fixed"
    return "failed"


def cycle(w, inp, args) -> str:
    img = ig.grab_checked(w)          # 顺带查「作弊风险」
    if args.shot:
        ok, buf = cv2.imencode(".png", img)
        buf.tofile(time.strftime("logs/early_%H%M%S.png"))

    # 1) 弹窗已经开着（上一轮残留 / 被别的流程触发）
    ok, ds, dp = find_popup(img)
    if ok:
        log(f"⚠️ 发现遗留弹窗（标题 {ds:.3f}）→ 直接点「是」")
        for i in range(3):
            if not click_yes(w, inp, img, args.dry):
                break
            if wait_popup_gone(w):
                log("   ✔ 弹窗已关闭")
                return "遗留弹窗→已确认"
            img = ig.grab(w)
            log(f"   弹窗还在，第 {i + 2} 次点")
        log("   ❌ 弹窗没能关掉")
        return "遗留弹窗→关闭失败"

    # 1.2) 自动挑战守护：连续多轮读到红字 0 才补刀（避免波间空档误判）
    global _auto_zero_streak, _auto_cooldown, _auto_rounds, _auto_gave_up
    if not getattr(args, "no_auto_guard", False):
        on_a, n_a = wv.is_auto_on(img)
        if on_a:
            if _auto_zero_streak >= AUTO_GUARD_CONFIRM:
                log(f"   自动挑战已恢复（连续 {_auto_zero_streak} 轮为 0 后回红）")
            _auto_zero_streak = 0
        else:
            _auto_zero_streak += 1
            if _auto_cooldown > 0:
                _auto_cooldown -= 1
            ready = (_auto_zero_streak >= AUTO_GUARD_CONFIRM
                     and _auto_cooldown == 0
                     and not _auto_gave_up
                     and _auto_rounds < AUTO_GUARD_MAX_ROUNDS)
            if ready:
                log(f"   连续 {_auto_zero_streak} 轮红字为 0 → 判定自动挑战真的掉了，补刀")
                _auto_rounds += 1
                _r = ensure_auto(w, inp, args)
                if _r in ("fixed", "on"):
                    _auto_zero_streak = 0
                    _auto_cooldown = 0
                    img = ig.grab(w)
                elif _r == "cleared":
                    # 主线已通关 → 本局不用再守自动挑战，也不重置 zero_streak（就是 0 才对）
                    _auto_zero_streak = 0
                    _auto_cooldown = 0
                    img = ig.grab(w)
                elif _r == "blank":
                    # 不在游戏内（结算/菜单）→ 这一轮什么都不做，下轮再看
                    pass
                else:
                    _auto_cooldown = AUTO_GUARD_COOLDOWN
                    if _auto_rounds >= AUTO_GUARD_MAX_ROUNDS:
                        _auto_gave_up = True
                        log("⚠️ 补刀多次都没出现「自动」→ 本局彻底停手"
                            "（主线多半已打满，红字消失只是波间空档）")
                    else:
                        log(f"   补刀没成 → 静默 {AUTO_GUARD_COOLDOWN} 轮再试")

    # 1.5) 「挑战存档」——挑战打完后的那一下，点了本轮就算完成
    ok, ss, sp = find_save(img)
    if ok:
        cx, cy = sp[0] + TPL_SAVE_W // 2, sp[1] + TPL_SAVE_H // 2
        log(f"💾 发现「挑战存档」score={ss:.3f} @({cx},{cy}) → 双击")
        if args.dry:
            log("   [dry] 不点击")
            return "发现存档(未点击)"
        for i in range(3):
            # 用户 2026-11-18 19:57：单击改双击
            inp.click(cx, cy, hold=0.06, clicks=2, gap=0.12)
            time.sleep(1.0)
            img = ig.grab(w)
            ok2, s2, p2 = find_save(img)
            if not ok2:
                log(f"   ✅ 存档按钮已消失（score={s2:.3f}）→ 本轮完成")
                if not args.no_slots:
                    # 面板已弹出 → 接着做「存档挑战」：12 格双击 x 2 轮
                    log("   ↳ 进入「存档挑战」：12 格双击 x2 轮")
                    try:
                        import save_challenge as sc
                        bad = sc.run(w, rounds=args.slots_rounds, gap=args.slots_gap)
                        log(f"   ✅ 存档挑战结束（失败 {bad} 格）" if bad
                            else "   ✅ 存档挑战结束（12 格 x 2 轮全部双击完成）")
                    except Exception as e:
                        log(f"   ❌ 存档挑战异常：{e!r}")
                return "已点挑战存档→完成"
            cx, cy = p2[0] + TPL_SAVE_W // 2, p2[1] + TPL_SAVE_H // 2
            log(f"   ↳ 按钮还在，第 {i + 2} 次点 @({cx},{cy})")
        log("   ❌ 3 次后存档按钮仍在")
        return "存档→未消失"

    # 2) 找图标
    hit, s, p, extra = find_icon(img)
    if not hit:
        log(f"未见「提前挑战」（score={s:.3f}"
            + (f"，黄/紫={extra[0]}/{extra[1]}" if extra[0] or extra[1] else "") + "）")
        return "未见"

    cx, cy = p[0] + TPL_ICON_W // 2, p[1] + TPL_ICON_H // 2
    log(f"🎯 发现「提前挑战」score={s:.3f} @({cx},{cy}) 黄/紫={extra[0]}/{extra[1]}")
    if args.dry:
        log("   [dry] 不点击")
        return "发现(未点击)"

    # 3) 双击图标（图标还在就重复，首次 + 重复 2 次 = 最多 3 次）
    #    2026-11-18 01:20 实测：直接 inp.click(clicks=2) 点不出弹窗（War3 对
    #    「鼠标刚移入就按下」不响应）。必须先 move 到图标上停一会儿让 hover 事件
    #    生效，再用 click_hover 双击 —— 这样弹窗 1 秒内就出来（score 1.000）。
    #    2026-11-18 20:02 用户要求：双击后图标还在 → 重复 2 次。
    ICON_TRIES = 3
    img2 = None
    popup_ok = False
    for attempt in range(ICON_TRIES):
        if attempt:
            # 重试前先确认图标还在；图标没了说明已经点到（或弹窗被别的流程吃掉）
            ok_i, s_i, p_i, _ex_i = find_icon(ig.grab(w))
            if not ok_i:
                log(f"   ↳ 图标已不在（score={s_i:.3f}）→ 不再重复")
                break
            cx, cy = p_i[0] + TPL_ICON_W // 2, p_i[1] + TPL_ICON_H // 2
            log(f"   ↳ 图标还在（score={s_i:.3f}）→ 重复双击 第 {attempt + 1}/{ICON_TRIES} 次 @({cx},{cy})")
        try:
            inp.move(*PARK)
            time.sleep(0.15)
            inp.move(cx, cy)
            time.sleep(0.7)
            inp.click_hover(cx, cy, hover=0.9, hold=0.08, clicks=2)
            log(f"   ↳ 已悬停后双击图标（第 {attempt + 1}/{ICON_TRIES} 次），等弹窗…")
        except Exception as e:
            log(f"   ❌ 双击失败（第 {attempt + 1}/{ICON_TRIES} 次）：{e}")
            continue

        t0 = time.time()
        while time.time() - t0 < 6.0:
            time.sleep(0.4)
            img2 = ig.grab(w)
            ok2, ds2, _ = find_popup(img2)
            if ok2:
                log(f"   ↳ 弹窗已出（标题 {ds2:.3f}，{time.time() - t0:.1f}s）")
                popup_ok = True
                break
        if popup_ok:
            break
        log(f"   ↳ 6s 内没等到弹窗（第 {attempt + 1}/{ICON_TRIES} 次）")

    if not popup_ok:
        log(f"   ❌ {ICON_TRIES} 次双击都没等到弹窗")
        return "双击后无弹窗"

    # 5) 点「是」
    for i in range(3):
        if not click_yes(w, inp, img2, args.dry):
            log("   ❌ 找不到「是」按钮")
            return "弹窗→找不到是"
        if wait_popup_gone(w):
            log("   ✔ 弹窗已关闭，提前挑战已开启")
            return "已开启提前挑战"
        img2 = ig.grab(w)
        log(f"   ↳ 弹窗还在，再点一次（第 {i + 2} 次）")
    log("   ❌ 3 次点击后弹窗仍未关闭")
    return "点是→未关闭"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=60.0, help="扫描间隔秒（默认 60）")
    ap.add_argument("--times", type=int, default=0, help="扫描轮数，0=无限")
    ap.add_argument("--once", action="store_true", help="等于 --times 1")
    ap.add_argument("--dry", action="store_true", help="只识别不点击")
    ap.add_argument("--no-auto-guard", action="store_true",
                    help="不守护自动挑战（掉了也不补右键）")
    ap.add_argument("--shot", action="store_true", help="每轮存截图")
    ap.add_argument("--stop-after-save", action="store_true",
                    help="点到「挑战存档」成功就退出（一轮 = 一次通关）")
    ap.add_argument("--no-slots", action="store_true",
                    help="点完「挑战存档」后不自动做「存档挑战」（12 格双击）")
    ap.add_argument("--slots-rounds", type=int, default=2, help="存档挑战整轮次数（默认 2）")
    ap.add_argument("--slots-gap", type=float, default=1.0, help="存档挑战每格间隔秒（默认 1.0）")
    ap.add_argument("--timeout", type=float, default=0,
                    help="总等待秒数上限，0=不限（配合 --stop-after-save，供通关循环编排用）")
    args = ap.parse_args()
    if args.once:
        args.times = 1

    global TPL_ICON_W, TPL_ICON_H, TPL_YES_W, TPL_YES_H, TPL_SAVE_W, TPL_SAVE_H
    t_icon, t_dlg, t_yes = load(TPL_ICON), load(TPL_DLG), load(TPL_YES)
    t_save = load(TPL_SAVE)
    globals()["TPL_ICON"] = t_icon
    globals()["TPL_DLG"] = t_dlg
    globals()["TPL_YES"] = t_yes
    globals()["TPL_SAVE"] = t_save
    TPL_ICON_H, TPL_ICON_W = t_icon.shape[:2]
    TPL_YES_H, TPL_YES_W = t_yes.shape[:2]
    TPL_SAVE_H, TPL_SAVE_W = t_save.shape[:2]

    log(f"启动监控：间隔 {args.interval:.0f}s，轮数 {'∞' if not args.times else args.times}"
        f"{'，dry' if args.dry else ''}；日志 {LOG_PATH}")

    w = None
    inp = None
    while w is None:
        try:
            w = wv.bind()
            inp = ga.Input(w, True, 0, False, ref_size=ig.REF)
            log(f"已绑定游戏窗口 hwnd={w.hwnd}")
        except Exception as e:
            log(f"等游戏窗口…（{e}）")
            time.sleep(10)
            if args.times == 1:
                return 3

    # 2026-11-19 20:00 用户定：登录守卫任务移进游戏监控 —— ④ 段期间由本进程承担
    # 守卫（每 3 秒标题检查 + 15 秒模板兜底），full_loop 主进程进 ④ 前会停掉自己的
    # 守卫线程，出 ④ 再恢复。这样消除了「主进程守卫 + 游戏监控」双监控并存；
    # 其余 6 个阶段（①②③⑤⑥⑦）仍由主进程守卫保护，全程无空窗。
    # 命中动作：本进程**立刻退出（exit 131）**，full_loop 按登录命中停手——
    # 监控语义本来就是「只看不点」，退出即停手，不需要掐别的进程。
    login_hit = {"ts": None}
    lg_stop = threading.Event()
    if _LG_OK:
        def _lg_on_hit(rec):
            login_hit["ts"] = rec.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S")
        threading.Thread(target=lg.watch_loop, args=(lg_stop, _lg_on_hit, 3.0,
                         "④ 游戏监控"), daemon=True, name="login-guard-in-watch").start()
        log("🛡 登录守卫由本进程承担（标题 3s / 模板兜底 15s；命中即退出停手）")
    else:
        log("（登录守卫没启用：login_guard 导入失败）")

    n = 0
    t_start = time.time()
    while True:
        if login_hit["ts"]:
            log(f"⛔ 检测到登录窗口（{login_hit['ts']}）→ 立刻退出停手"
                f"（没点任何东西），full_loop 会按登录命中处理")
            return 131
        if args.timeout and time.time() - t_start > args.timeout:
            log(f"⏱ 已等 {args.timeout:.0f}s 仍未等到「挑战存档」→ 超时退出")
            return 2
        n += 1
        try:
            res = cycle(w, inp, args)
            log(f"第 {n} 轮 → {res}")
        except ga.WindowLost:
            log("游戏窗口已关闭 → 退出监控")
            return 0
        except RuntimeError as e:          # grab_checked 命中作弊提示时抛这个
            log(f"⛔ 中断：{e}")
            return 3
        except Exception as e:
            log(f"第 {n} 轮异常：{e!r}")
        if args.stop_after_save and res == "已点挑战存档→完成":
            log("🏁 挑战存档已完成 → 本局通关，退出监控")
            return 0
        if args.times and n >= args.times:
            break
        # 大睡拆成 0.5s 小步：守卫命中后最多 0.5 秒就退出，不用干等完整一个 interval
        _left = args.interval
        while _left > 0:
            if login_hit["ts"]:
                break
            _step = min(0.5, _left)
            time.sleep(_step)
            _left -= _step
    lg_stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
