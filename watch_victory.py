# -*- coding: utf-8 -*-
"""
结算收尾：等结算界面 → 点「继续观战」→ 退出游戏

用户定义（2026-11-16 13:32）：监控等待出现胜利了就可以点击 继续观战，操作退出游戏，完成这次循环了。
用户补充（2026-11-18 09:49）：秘境挑战可能存在失败问题，如果遇见直接点击继续观战，操作退出游戏。

所以这是「一次循环」的最后一环：

    （前面）点「是」开启秘境 → 自动战斗
    ① 等结算界面出现（判据 = 「继续观战」按钮，比找「胜利」两个字稳）
    ② 区分「胜利」/「失败」（失败时点「继续观战」只关面板，主游戏还在继续）
    ③ 点「继续观战」→ 回到地图
    ④ 退出游戏（复用 configs/war3_exit.json 的 F10 → E → X → X 流程）
    → 本次循环结束

结算界面按钮坐标（客户区 1690x921，实测）：
    退出游戏   (748, 526)
    继续观战   (937, 525)   模板 btn_watch_continue.png  175x41

⚠ 关键坑 1：结算界面的按钮 **直接 move+click 点不动**。
   实测连点 3 次，匹配分数一直卡在 0.890 —— 那是按钮的 hover 高亮态，说明鼠标到了
   但 War3 没把它当成一次有效点击。必须 move 过去停 0.6~0.8s 再按下（Input.click_hover）。

⚠ 关键坑 2：**窗口没有前台焦点时，点了也没反应**（2026-11-18 09:51 实测）。
   SetForegroundWindow 之后再用 click_hover 才生效，所以每次点击前都先 focus。

失败判据（2026-11-18 实测，N8 不死族秘境那局）：
    结算界面中间会显示蓝白色大字「失败」，区域 (740,228)-(950,278)。
      失败帧：2859 ~ 2877 个蓝白像素
      正常帧（37 张时间线截图）：全 0
      平台界面截图：最高 172
    阈值取 900，区分度足够。找到「失败」就说明是失败结算，不是胜利。

用法：
    python watch_victory.py                 # 每 3s 扫一次，等到结算就收尾（点继续观战 + 退出）
    python watch_victory.py --interval 5    # 改扫描间隔
    python watch_victory.py --no-quit       # 只点「继续观战」，不退出游戏
    python watch_victory.py --on-fail wait  # 遇到失败不退出，关掉面板后继续等真正的胜利
    python watch_victory.py --once          # 只扫一轮（调试）
    python watch_victory.py --timeout 1800  # 最多等 30 分钟，超时退出

返回码：
    0 = 胜利 → 继续观战 → 退出
    1 = 结算按钮点不动 / 退出流程失败
    2 = 超时没等到结算
    3 = 游戏窗口不在
    4 = 遭遇失败 → 继续观战 → 退出（收尾本身是成功的）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

import cv2
import numpy as np

import game_auto as ga
import in_game as ig
import wave_auto as wv

TPL_CONT = "templates/btn_watch_continue.png"
SCORE_CONT = 0.85
# 2026-11-25 W7 现场：结算界面的「继续观战」按钮分数从 0.524 慢慢爬到 0.830 就停住，
# 0.85 永远够不到 → 收尾 360s 超时、既没点继续观战也没退游戏。
# 只对 Win7 放宽（W10 仍是 0.85）。
SCORE_CONT_WIN7 = 0.80
CONT_XY = (937, 525)          # 「继续观战」中心
HOVER = 0.8                   # 点结算按钮前必须停这么久

REF = (1690, 921)             # 客户区基准尺寸
FAIL_ROI = (740, 228, 950, 278)   # 「失败」大蓝白字所在区域（基准坐标）
FAIL_MIN = 900                # 蓝白像素阈值：失败帧 2859+，正常帧 0

CFG_QUIT = "configs/war3_exit.json"

RC_OK, RC_STUCK, RC_TIMEOUT, RC_NOGAME, RC_FAILED = 0, 1, 2, 3, 4


def _win7() -> bool:
    try:
        import sysenv
        return bool(sysenv.is_win7())
    except Exception:
        return False


def score_cont() -> float:
    """「继续观战」阈值：Win7 放宽到 0.80，其他系统仍是 0.85。"""
    return SCORE_CONT_WIN7 if _win7() else SCORE_CONT


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_tpl(path: str):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def find_victory(img, tpl):
    """返回 (是否结算界面, 分数, 位置)"""
    if tpl is None:
        return False, -1.0, (0, 0)
    r = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, ml = cv2.minMaxLoc(r)
    return mx >= score_cont(), float(mx), ml


def fail_title_pixels(img) -> int:
    """结算界面中间那个大字是「失败」还是「胜利」：失败是蓝白色，胜利是金黄色。

    只数蓝白像素（b>170,g>170,r>150 且 蓝≥红），失败帧 2859+，正常帧 0。
    """
    if img is None:
        return 0
    h, w = img.shape[:2]
    sx, sy = w / REF[0], h / REF[1]
    x0, y0, x1, y1 = (int(FAIL_ROI[0] * sx), int(FAIL_ROI[1] * sy),
                      int(FAIL_ROI[2] * sx), int(FAIL_ROI[3] * sy))
    roi = img[max(0, y0):y1, max(0, x0):x1]
    if roi.size == 0:
        return 0
    b = roi[:, :, 0].astype(int)
    g = roi[:, :, 1].astype(int)
    r = roi[:, :, 2].astype(int)
    return int(((b > 170) & (g > 170) & (r > 150) & (b >= r - 10)).sum())


def click_continue(w, inp, tpl, tries: int = 4) -> bool:
    """点「继续观战」直到结算界面消失。

    两个坑都要绕：① 必须 hover 后再按下；② 窗口没有前台焦点时点击会被吞。
    """
    for n in range(1, tries + 1):
        try:
            w.focus()                          # 没有前台焦点 → War3 吞点击
        except Exception:
            pass
        time.sleep(0.35)
        inp.click_hover(*CONT_XY, hover=HOVER, hold=0.12)
        time.sleep(1.2)
        inp.move(1200, 190)                    # 挪开鼠标，避免 hover 高亮影响判分
        time.sleep(0.3)
        img = ig.grab_checked(w)
        ok, s, ml = find_victory(img, tpl)
        log(f"   第 {n} 次点「继续观战」→ score={s:.3f} at {ml}")
        if not ok:
            return True
    return False


def quit_game() -> int:
    log("   退出游戏：F10 → E → X → X（configs/war3_exit.json）")
    p = subprocess.run(
        [sys.executable, "-W", "ignore", "game_auto.py", "run",
         "--config", CFG_QUIT, "--task", "quit_game", "--autostart"],
        capture_output=True)
    out = (p.stdout or b"").decode("utf-8", "ignore")
    for line in out.strip().splitlines():
        if any(k in line for k in ("①", "②", "③", "④", "窗口已关闭", "结束", "❌")):
            log("   " + line.split("] ", 1)[-1])
    return p.returncode


def main():
    ap = argparse.ArgumentParser(description="结算 → 继续观战 → 退出游戏（自动区分胜利 / 失败）")
    ap.add_argument("--interval", type=float, default=3.0, help="扫描间隔秒（默认 3）")
    ap.add_argument("--timeout", type=float, default=1800, help="最长等待秒（默认 1800）")
    ap.add_argument("--tries", type=int, default=4, help="点继续观战最多几次")
    ap.add_argument("--no-quit", action="store_true", help="只点继续观战，不退出游戏")
    ap.add_argument("--on-fail", choices=("quit", "wait"), default="quit",
                    help="遇到失败结算：quit=点继续观战后退出游戏（默认）；"
                         "wait=只关面板，继续等真正的胜利")
    ap.add_argument("--once", action="store_true", help="只扫一轮")
    args = ap.parse_args()

    tpl = load_tpl(TPL_CONT)
    w = wv.bind()
    if w is None:
        log("没找到 War3 窗口 —— 游戏没开，先开一局再来")
        return RC_NOGAME
    inp = ga.Input(w, True, 0, False, ref_size=ig.REF)
    t0 = time.time()
    log(f"开始监控结算界面（{args.interval}s 一次，上限 {args.timeout:.0f}s）")

    fails = 0
    while True:
        if time.time() - t0 > args.timeout:
            log(f"超时 {args.timeout:.0f}s 没等到结算，退出")
            return RC_TIMEOUT
        try:
            img = ig.grab_checked(w)          # 顺带查作弊提示
        except Exception as e:
            log(f"截图失败（游戏可能已关闭）：{e!r}")
            return RC_NOGAME

        ok, s, ml = find_victory(img, tpl)
        if ok:
            fp = fail_title_pixels(img)
            is_fail = fp >= FAIL_MIN
            if is_fail:
                fails += 1
                log(f"⚠ 检测到【失败】结算（score={s:.3f} @{ml}，蓝白像素={fp}）"
                    f" 第 {fails} 次 —— 秘境挑战 / 主游戏这场没打过")
            else:
                log(f"✅ 检测到【胜利】结算（score={s:.3f} @{ml}，蓝白像素={fp}）")

            if not click_continue(w, inp, tpl, args.tries):
                log("❌ 「继续观战」点了多次仍没离开结算界面")
                return RC_STUCK
            log("✅ 已回到地图")

            if is_fail and args.on_fail == "wait" and not args.no_quit:
                # 用户可选用法：失败不退出，关掉面板后继续等真正的胜利（波数还在推进）
                log("   按 --on-fail wait：面板已关，继续等主游戏的胜利结算…")
                if args.once:
                    return RC_FAILED
                time.sleep(args.interval)
                continue

            if args.no_quit:
                log("按 --no-quit 不退出游戏，监控结束")
                return RC_FAILED if is_fail else RC_OK
            rc = quit_game()
            if rc == 0:
                if is_fail:
                    log("✅ 本次循环收尾完成（失败 → 继续观战 → 退出游戏）")
                    return RC_FAILED
                log("✅ 本次循环完成（胜利 → 继续观战 → 退出游戏）")
                return RC_OK
            log(f"❌ 退出流程返回 {rc}")
            return RC_STUCK
        else:
            log(f"未见结算（score={s:.3f}）")

        if args.once:
            return RC_OK
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
