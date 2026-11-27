# -*- coding: utf-8 -*-
"""
「存档挑战」—— 点开「挑战存档」后的 12 格双击（用户 2026-11-16 12:31 定义）

用户原话：12 个格子，每个格子双击一次，间隔 1 秒，循环 2 次；这个操作归纳为「存档挑战」。

面板位置（客户区 1690x921，实测）：
  4 列 x 3 行，格子约 52x50，列/行间距都是 76px
  第 1 格（左上，编号 1）中心 (1371, 733)
  第 n 格：col=(n-1)%4 → x=1371+76*col；row=(n-1)//4 → y=733+76*row
  第 12 格（右下）中心 (1599, 885)

实测校验（当前这局）：
  第 1 行 4 格都有英雄头像（std 55~66）
  第 2 行前 2 格有头像、后 2 格空（mean 18 / std 1）
  第 3 行 4 格全空（mean 19 / std 8）

注意：和「是」「挑战存档」一样，双击也可能被 War3 的输入锁定期吞掉，
所以每格双击后隔 gap 秒再走下一格（默认 1.0s），整轮跑 2 遍兜住偶发漏点。

用法：
  python save_challenge.py                 # 12 格 x 2 轮，间隔 1s（约 25s）
  python save_challenge.py --rounds 1      # 只跑 1 轮
  python save_challenge.py --gap 0.6       # 间隔 0.6s（快一点）
  python save_challenge.py --dry           # 只打印坐标不点击
  python save_challenge.py --check         # 只检查面板在不在
"""
from __future__ import annotations

import argparse
import sys
import time

import cv2
import numpy as np

import game_auto as ga
import in_game as ig
import wave_auto as wv

# ---- 12 格坐标（编号 1..12，左→右、上→下）----
X0, Y0, PITCH = 1371, 733, 76
# 2026-11-23 13:45 用户要求：**只点第 12 格（右下 1599,885）**，其余 11 格不要
CELLS = [(1599, 885)]

TPL_TITLE = "templates/tpl_save_panel_title.png"
SCORE_TITLE = 0.82          # Win10 / 默认阈值（原有值，不动）
SCORE_TITLE_WIN7 = 0.75     # 仅 Win7：同一块面板标题在 6.1.7601 上实测只有 0.769


def _win7() -> bool:
    try:
        import sysenv
        return bool(sysenv.is_win7())
    except Exception:
        return False


def _threshold() -> float:
    """Win7 才放宽；其他系统仍是 0.82。

    2026-11-25 W7 实测：面板明明开着，标题模板只配到 0.769（<0.82）→ 被判「面板没开」
    → D 阶段中止 → 清场把游戏退了。W7 字体渲染和 W10 不同导致的掉分。
    阈值可按档位改：core/configs/os_profile.json 的 save_panel_score。
    """
    if not _win7():
        return SCORE_TITLE
    try:
        import sysenv
        return float(sysenv.settings().get("save_panel_score", SCORE_TITLE_WIN7))
    except Exception:
        return SCORE_TITLE_WIN7


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def panel_open(img, threshold: float = None) -> tuple:
    th = _threshold() if threshold is None else threshold
    try:
        tpl = cv2.imdecode(np.fromfile(TPL_TITLE, dtype=np.uint8), cv2.IMREAD_COLOR)
        if tpl is None:
            return True, -1.0        # 没模板就当开着，交给调用方决定
        r = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, _ = cv2.minMaxLoc(r)
        return mx >= th, float(mx)
    except Exception:
        return True, -1.0


def run(w, rounds: int = 2, gap: float = 1.0, dry: bool = False,
        require_panel: bool = False) -> int:
    """执行 12 格 x rounds 轮双击。返回失败格子数。"""
    img = ig.grab(w)
    ok, score = panel_open(img)
    if not ok and _win7():
        # W7 面板刚弹出来时会有半透明渐入，单帧容易压在阈值下 → 隔 0.6s 补一帧取最高
        time.sleep(0.6)
        try:
            ok2, score2 = panel_open(ig.grab(w))
            log(f"   ↻ Win7 复检：{score2:.3f}（首帧 {score:.3f}，阈值 {_threshold():.2f}）")
            if score2 > score:
                ok, score = ok2, score2
        except Exception as e:
            log(f"   ↻ Win7 复检失败：{e!r}")
    if not ok:
        log(f"⚠️ 没检测到「存档挑战」面板（标题 {score:.3f}，阈值 {_threshold():.2f}）")
        if require_panel:
            return -1
    inp = None if dry else ga.Input(w, True, 0, False, ref_size=ig.REF)

    bad = 0
    for r in range(1, rounds + 1):
        log(f"—— 第 {r}/{rounds} 轮：{len(CELLS)} 格双击 ——")
        for n, (x, y) in enumerate(CELLS, 1):
            if dry:
                log(f"   [{r}] {n:>2} 格 [dry] 双击 ({x},{y})")
            else:
                try:
                    inp.click(x, y, clicks=2, hold=0.07)
                    log(f"   [{r}] {n:>2} 格 双击 ({x},{y})")
                except Exception as e:
                    bad += 1
                    log(f"   [{r}] {n:>2} 格 失败：{e!r}")
            time.sleep(gap)
    # 2026-11-23 13:48 用户要求：点完后延迟 15 秒，等游戏把存档流程走完
    log(f"   ⏳ 等 15 秒（存档流程结算）…")
    time.sleep(15)
    log(f"存档挑战完成：{rounds} 轮 x {len(CELLS)} 格" + (f"，失败 {bad} 格" if bad else ""))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2, help="整轮次数（默认 2）")
    ap.add_argument("--gap", type=float, default=1.0, help="每格间隔秒（默认 1.0）")
    ap.add_argument("--dry", action="store_true", help="只打印坐标")
    ap.add_argument("--check", action="store_true", help="只检查面板是否打开")
    ap.add_argument("--require-panel", action="store_true", help="面板没开就报错退出")
    args = ap.parse_args()

    w = wv.bind()
    if args.check:
        img = ig.grab(w)
        ok, score = panel_open(img)
        log(f"存档挑战面板：{'✅ 已打开' if ok else '❌ 未打开'}（标题 {score:.3f}）")
        return 0 if ok else 2
    rc = run(w, args.rounds, args.gap, args.dry, args.require_panel)
    if rc == -1:
        log("面板没开 → 退出码 4（供通关循环编排区分「跳过」与「失败」）")
        return 4
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
