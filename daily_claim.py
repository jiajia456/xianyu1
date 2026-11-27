# -*- coding: utf-8 -*-
"""【签到领券】一键入口（用户 2026-11-17 02:16 定名）

「签到领券」= 平台每日两件小事，按顺序做完：
  ① 签到：点顶部「签到」→ 弹窗里「立即签到」双击（已签则看到「今日已签到」）→ 停 5 秒 → 关窗
  ② 领券：tab「任务」有红点就点进去 → 左侧带红点的分类逐个「一键领取」→「确定」，直到没红点

两步都能独立跑（daily_sign.py / task_dot.py + claim_tasks.py），这个文件只是串起来，
以后说「跑签到领券」就执行它。

用法：
  python daily_claim.py            # 签到 + 领券
  python daily_claim.py --sign-only
  python daily_claim.py --task-only
  python daily_claim.py --dry      # 只看今天签没签、任务有没有红点，不点任何东西
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import task_dot as td
import popup_guard as pg

HERE = os.path.dirname(os.path.abspath(__file__))


def ensure_detail(tag=""):
    """把平台拉回「地图详情」页。

    踩坑（2026-11-17 09:23）：大循环里刚跑完通关循环就调本脚本，平台还停在
    结算/战绩页，顶部「签到」和 tab「任务」都匹配不到 → 签到返回 4、领券直接跳过，
    等于整段【签到领券】白跑。所以两步之前都先回详情页再重试一次。
    """
    if tag:
        print(f"   ↻ {tag}：先回「地图详情」页再试")
    try:
        import plat_home as ph
        r = ph.ensure_detail(verbose=True)
        if r.get("ok"):
            return True
        print(f"   (形态校正没成功：{r.get('reason')}，交给 pipeline 自己切页，不再盲点 tab)")
    except Exception as e:
        print(f"   (plat_home 不可用：{e!r})")
    try:
        # 2026-11-18：不再调用会盲点 tab 的旧逻辑（平台停在游戏大厅时会点到充值横幅），
        # 只做一次无副作用的形态检查，剩下的交给 pipeline ① 搜地图自己切页。
        import fishing_mode as fm
        fm.ensure_detail_page()
        time.sleep(1.0)
        return True
    except Exception as e:
        print(f"   (回详情页失败：{e!r})")
        return False


def run(script, *args):
    cmd = [sys.executable, os.path.join(HERE, script), *args]
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        print(out)
    if err:
        print("[stderr]", err)
    return r.returncode


def check_login(label=""):
    """登录窗口守卫（只检测，不动手）"""
    try:
        import login_guard as lg
    except Exception:
        return None
    return lg.check(label or "签到领券")


def login_abort(where=""):
    ts = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    try:
        import login_guard as lg
        rsn = lg.last_reason()
    except Exception:
        rsn = "检测到登录窗口"
    print("=" * 56)
    print(f"⛔ {rsn}：{ts}")
    print(f"   位置：{where or '签到领券'}")
    print("   已停止任务，未做任何操作（没点登录/确定、没关窗口、没退游戏）")
    print("   记录：logs/login_events.jsonl")
    print("=" * 56)
    return 131


def esc_before_daily(times=2, gap=3.0):
    """进签到/领券之前（已经回到「地图详情」页）按 ESC 清场（用户 2026-11-26 定）。

    时机：`ensure_detail()` 把平台拉回地图详情页之后、点「签到」/「任务」之前。
    这时屏幕上可能还留着上一局退出后的结算浮层、活动横幅、残留提示 ——
    按两下 ESC 清掉，两次之间间隔 3 秒，避免它们挡住顶部「签到」和 tab「任务」。
    例外与 pipeline 的 esc_safe 一致：屏幕上有「地图已更新」提示时**不按**，
    按了提示消失、平台就不下载地图 → 后面开局 War3 秒退。
    """
    try:
        import map_update as mu
        if mu.has_update_window() and mu.find(verbose=False):
            print("   ⏭ 屏幕上有「地图已更新」提示 → 跳过 ESC（按了地图不会下载）")
            return
    except Exception:
        pass
    hwnd = td.find_platform()
    if not hwnd:
        print("   (没找到平台窗口 → 跳过 ESC)")
        return
    try:
        import win32api
        import win32con
    except Exception as e:
        print(f"   (ESC 不可用：{e!r})")
        return
    for i in range(max(1, times)):
        try:
            pg.force_foreground(hwnd)     # 焦点不在平台时 ESC 会打到别的窗口
        except Exception:
            pass
        try:
            win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
            time.sleep(0.05)
            win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0)
            print(f"   🔙 进签到前 ESC（{i + 1}/{times}）")
        except Exception as e:
            print(f"   (ESC 失败：{e!r})")
            return
        if i + 1 < times:
            time.sleep(gap)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sign-only", action="store_true")
    ap.add_argument("--task-only", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    pg.enable_dpi_awareness()
    print("=" * 56)
    print("【签到领券】开始 " + __import__("time").strftime("%H:%M:%S"))
    print("=" * 56)

    if check_login("签到领券开始前"):
        return login_abort("签到领券开始前")
    # 2026-11-18 13:56 用户指出：打完一局「退出游戏」后屏幕停在**游戏房间**窗口，
    # 里面没有顶部「签到」和 tab「任务」→ 先带回「地图详情」页再往下走
    # 2026-11-19 降 CPU：这里确认到的详情页 hwnd 往下传给 daily_sign，
    # 免得子进程 1 秒后又对同一个窗口跑一遍全窗扫描（实测 0.5s/次）。
    _detail_hwnd = 0
    try:
        import plat_home as ph
        _r = ph.ensure_detail(verbose=True)
        if _r.get("ok"):
            _detail_hwnd = int(_r.get("hwnd") or 0)
        else:
            print(f"   \u26a0\ufe0f 平台没回到「地图详情」页（{_r.get('reason')}）——后面可能找不到按钮")
    except Exception as _e:
        print(f"   (平台形态校正跳过：{_e!r})")
    # 用户 2026-11-26：**进签到/领券之前**（已回详情页）按两下 ESC，两次间隔 3 秒，
    # 清掉上一局退出后的结算浮层/活动横幅，别让它们挡住顶部「签到」和 tab「任务」。
    if not a.dry:
        esc_before_daily(times=2, gap=3.0)


    rc_sign = rc_task = 0

    if not a.task_only:
        print("\n① 签到")
        args = ("--check",) if a.dry else ()
        args_hw = (*args, "--detail-hwnd", str(_detail_hwnd)) if _detail_hwnd else args
        rc_sign = run("daily_sign.py", *args_hw)
        if rc_sign == 4 and not a.dry and ensure_detail("签到按钮没找到"):
            # 重试时不再传旧句柄：中间可能刚退过房，那个 hwnd 已经不代表详情页了
            rc_sign = run("daily_sign.py", *args)
        print(f"   退出码 {rc_sign}")
    if a.sign_only:
        return rc_sign

    if check_login("领券前"):
        return login_abort("领券前")

    # 2026-11-19 00:07 实测：退出游戏后平台随机弹「首充礼包」独立小窗（924x562），
    # 它是**模态**的 —— 弹着的时候点「任务」tab 没反应。领券前先按用户教的点 X 关掉。
    if not a.dry:
        try:
            import ad_guard
            if ad_guard.close(verbose=True) == "closed":
                print("   \u2713 先关掉了一个广告弹窗（它会吃掉「任务」tab 的点击）")
        except Exception as _e:
            print(f"   (广告守卫跳过：{_e!r})")

    print("\n② 任务领券")
    hwnd = td.find_platform()
    if not hwnd:
        print("   ❌ 没找到平台窗口")
        return 2
    small, k = td.snap1080(hwnd)
    has_dot, center, score = td.detect(small) if small is not None else (None, None, 0)
    if center is None and not a.dry:
        if ensure_detail("没找到「任务」tab"):
            hwnd = td.find_platform()
            if hwnd:
                small, k = td.snap1080(hwnd)
                has_dot, center, score = td.detect(small) if small is not None else (None, None, 0)
    if center is None:
        print(f"   ⚠️ 没找到「任务」tab（score={score:.3f}），当前可能不在地图详情页 —— 跳过领券")
        return rc_sign
    print(f"   「任务」tab 1080 基准{center} 红点={'有' if has_dot else '没有'}")
    if not has_dot:
        # 用户规则（02:25）：任务无红点就直接判定「今天已完成」，跳过，不进任务页
        print("   ✅ 任务无红点 → 直接判定今日任务已完成，跳过领券")
        return rc_sign
    if a.dry:
        print("   （--dry 只检测，不点）")
        return rc_sign

    rc1 = run("task_dot.py")
    if rc1:
        print("   ⚠️ 没能进入「任务」页（task_dot 失败）→ 跳过领券，避免在错误页面误点")
        rc2 = rc1
    else:
        rc2 = run("claim_tasks.py")
    rc_task = rc1 or rc2
    print(f"   退出码 task_dot={rc1} claim_tasks={rc2}")

    print("\n" + "=" * 56)
    print(f"【签到领券】结束 签到={rc_sign} 领券={rc_task}")
    print("=" * 56)
    return rc_sign or rc_task


if __name__ == "__main__":
    raise SystemExit(main())
