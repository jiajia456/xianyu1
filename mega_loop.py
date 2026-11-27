# -*- coding: utf-8 -*-
"""三大固定流程的循环调度（用户 2026-11-17 02:30 需求）

需求原话：「我要三个固定流程做个循环：通关循环多少局 - 钓鱼模式多少局循环交替，
只中间产生多少局去执行签到领券，领完继续循环」→ 参数全部可自定义（后期做软件界面要配）。

一条大循环长这样：
  第 1 轮
    ├─ 通关循环 ×N 局（每满 K 局插一次【签到领券】，领完接着跑）
    ├─ 钓鱼模式 ×M 次（每次安静等 H 小时）
    └─ …
  第 2 轮 …

三个固定流程：
  通关循环 = full_loop.py（N8+ 海岛↔不死交替 / N1~N7 跳过秘境直退）
  钓鱼模式 = fishing_mode.py（进大厅点钓鱼 → 静等 H 小时 → 退出）
  签到领券 = daily_claim.py（签到 + 任务红点一键领取）

用法（全部可自定义）：
  python mega_loop.py                                     # 默认 3 局 : 1 次钓鱼，每 3 局签到领券，无限轮
  python mega_loop.py --clear-games 5 --fishing-times 2 --claim-every 5 --fishing-hours 5
  python mega_loop.py --clear-games 3 --fishing-times 1 --claim-every 0    # 0 = 不插签到领券
  python mega_loop.py --loops 2                            # 只跑 2 轮
  python mega_loop.py --dry                                # 只打印计划，不执行

注意：
  - 「局数」指的是**通关循环的局**（每局 13~15 分钟）；钓鱼按「次」算，每次固定 H 小时。
  - 签到领券按**累计局数**触发（跨轮累计），所以 K > N 也能在下一轮凑够时触发。
  - 签到领券本身每天只需一次：当天第二次起会自动判定「已签到 / 任务无红点」直接跳过，约 15~30 秒。
  - 每段执行结果追加到 logs/mega_journal.jsonl，长跑后可直接查跑到哪了。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL = os.path.join(HERE, "logs", "mega_journal.jsonl")

LOGIN = {"ts": None}      # 检测到登录窗口的时间；一旦有值就停手，什么都不做
CUR = {"p": None}         # 当前子进程（登录窗出现时立即掐掉）
RC_LOGIN = 131


def login_watch_start():
    try:
        import login_guard as lg
    except Exception as e:
        print(f"   (登录窗口守卫没启用：{e!r})", flush=True)
        return
    import threading

    def on_hit(rec):
        if LOGIN["ts"]:
            return
        LOGIN["ts"] = rec.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S")
        p = CUR.get("p")
        if p is not None and p.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True)
            except Exception:
                pass

    threading.Thread(target=lg.watch_loop,
                     args=(threading.Event(), on_hit, 3.0, "大循环"),
                     daemon=True, name="login-guard").start()
    print("🛡 登录窗口守卫已启动（命中即停手，不做任何操作）", flush=True)


def check_login(label="") -> bool:
    if LOGIN["ts"]:
        return True
    try:
        import login_guard as lg
    except Exception:
        return False
    rec = lg.check(label or "大循环")
    if rec:
        LOGIN["ts"] = rec.get("ts")
        return True
    return False


def login_abort(where=""):
    ts = LOGIN["ts"] or time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        import login_guard as lg
        rsn = lg.last_reason()
    except Exception:
        rsn = "检测到登录窗口"
    stamp("=" * 60)
    stamp(f"⛔ {rsn}：{ts}")
    stamp(f"   位置：{where or '大循环'}")
    stamp("   已停止任务，未做任何操作（没点登录/确定、没关窗口、没退游戏）")
    stamp("   记录：logs/login_events.jsonl")
    stamp("=" * 60)
    return RC_LOGIN


def stamp(msg):
    print(f"\n[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def journal(kind, **kw):
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind}
    rec.update(kw)
    try:
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def run(script, args, label):
    """子进程执行一个固定流程，stdout 直通（长跑能实时看进度）

    登录窗口一旦出现 → 立刻掐掉子进程并返回 131（不做任何补救动作）。
    """
    if LOGIN["ts"]:
        return RC_LOGIN
    cmd = [sys.executable, os.path.join(HERE, script), *args]
    stamp(f"▶ {label}")
    print("   " + " ".join([script, *args]), flush=True)
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, cwd=HERE)
        CUR["p"] = p
        while p.poll() is None:
            if LOGIN["ts"]:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True)
                except Exception:
                    pass
                break
            time.sleep(0.3)
        rc = p.returncode
    except KeyboardInterrupt:
        stamp("⛔ 用户中断")
        raise
    except Exception as e:
        print(f"   异常：{e!r}", flush=True)
        rc = 1
    finally:
        CUR["p"] = None
    dur = int(time.time() - t0)
    print(f"   {'✅' if rc == 0 else '❌'} {label} 结束 exit={rc} 用时 {dur // 60}分{dur % 60}秒",
          flush=True)
    journal("segment", label=label, script=script, args=args, rc=rc, seconds=dur)
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear-games", type=int, default=3, metavar="N",
                    help="每轮通关循环跑几局（默认 3）")
    ap.add_argument("--fishing-times", type=int, default=1, metavar="M",
                    help="每轮钓鱼模式跑几次（默认 1，每次 = --fishing-hours 小时）")
    ap.add_argument("--claim-every", type=int, default=3, metavar="K",
                    help="每累计 K 局插一次【签到领券】（默认 3；0 = 不插）")
    ap.add_argument("--loops", type=int, default=0, metavar="R",
                    help="大循环跑几轮（默认 0 = 无限）")
    ap.add_argument("--fishing-hours", type=float, default=5.0, metavar="H",
                    help="每次钓鱼等几小时（默认 5）")
    ap.add_argument("--stop-on-fail", action="store_true", help="任何一段失败就停（默认继续跑）")
    ap.add_argument("--dry", action="store_true", help="只打印计划")
    a = ap.parse_args()

    N = max(0, a.clear_games)
    M = max(0, a.fishing_times)
    K = max(0, a.claim_every)
    R = max(0, a.loops)

    stamp("=" * 60)
    stamp("【三大流程循环】")
    stamp(f"  每轮：通关循环 {N} 局 → 钓鱼模式 {M} 次（每次 {a.fishing_hours:g} 小时）")
    stamp(f"  签到领券：每累计 {K if K else '∞'} 局插一次" + ("（关闭）" if not K else ""))
    stamp(f"  大循环：{'无限' if not R else f'{R} 轮'}")
    stamp("=" * 60)
    if a.dry:
        games_total = 0
        for lp in range(1, (R or 2) + 1):
            print(f"  第 {lp} 轮：", end="")
            parts = []
            remain = N
            while remain > 0:
                seg = min(K - (games_total % K), remain) if K else remain
                parts.append(f"通关×{seg}")
                games_total += seg
                remain -= seg
                if K and games_total % K == 0:
                    parts.append("签到领券")
            if M:
                parts.append(f"钓鱼×{M}")
            print(" → ".join(parts) if parts else "（空）")
        print("  （--dry 只打印，不执行）")
        return 0

    games_total = 0
    lp = 0
    login_watch_start()
    try:
        while not R or lp < R:
            lp += 1
            if check_login(f"第 {lp} 轮开始前"):
                rc = login_abort(f"第 {lp} 轮开始前")
                journal("login_stop", loop=lp, ts_hit=LOGIN["ts"])
                return rc
            stamp("=" * 60)
            stamp(f"🔁 第 {lp} 轮{'（无限循环）' if not R else f'/{R}'} 开始")
            stamp("=" * 60)
            journal("loop_start", loop=lp)

            # 开局前先回「地图详情」页（平台停在任务页/别的 tab 时开局容易点空）
            try:
                import fishing_mode as fm
                fm.ensure_detail_page()
            except Exception as e:
                print(f"   (回详情页跳过：{e!r})", flush=True)

            # ---- 通关循环（按 K 分段，段间插签到领券）----
            remain = N
            while remain > 0:
                seg = min(K - (games_total % K), remain) if K else remain
                rc = run("full_loop.py", ["--rounds", str(seg)],
                         f"通关循环 ×{seg} 局（累计 {games_total + seg} 局）")
                games_total += seg
                remain -= seg
                if LOGIN["ts"]:
                    journal("login_stop", loop=lp, ts_hit=LOGIN["ts"])
                    return login_abort("通关循环期间")
                if a.stop_on_fail and rc != 0:
                    stamp("⛔ --stop-on-fail：通关循环失败，停止")
                    return rc
                if K and games_total % K == 0:
                    stamp(f"💠 累计 {games_total} 局 → 执行【签到领券】")
                    rc2 = run("daily_claim.py", [], "签到领券")
                    if LOGIN["ts"]:
                        journal("login_stop", loop=lp, ts_hit=LOGIN["ts"])
                        return login_abort("签到领券期间")
                    if a.stop_on_fail and rc2 != 0:
                        stamp("⛔ --stop-on-fail：签到领券失败，停止")
                        return rc2

            # ---- 钓鱼模式 ----
            for j in range(1, M + 1):
                rc = run("fishing_mode.py", ["--hours", str(a.fishing_hours)],
                         f"钓鱼模式 第 {j}/{M} 次（{a.fishing_hours:g} 小时）")
                if LOGIN["ts"]:
                    journal("login_stop", loop=lp, ts_hit=LOGIN["ts"])
                    return login_abort("钓鱼模式期间")
                if a.stop_on_fail and rc != 0:
                    stamp("⛔ --stop-on-fail：钓鱼模式失败，停止")
                    return rc

            journal("loop_end", loop=lp, games_total=games_total)
            stamp(f"🔁 第 {lp} 轮结束（累计通关 {games_total} 局）")

        stamp("🏁 全部轮次完成")
    except KeyboardInterrupt:
        stamp("⛔ 收到中断，退出")
        journal("interrupted", games_total=games_total)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
