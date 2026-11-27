# -*- coding: utf-8 -*-
"""平台弹窗常驻监控 —— 一出现就判断并点掉（不关闭，一直盯着）

为什么要常驻：2026-11-17 21:26 实测，用户退出游戏后平台**约 3 秒**才冒出
「216U币 红包」那个 415x448 独立小窗。任何「某个时机扫一次」的写法都可能
正好打空（扫完它才出现）；常驻进程每 0.8 秒看一次，出现即处理，没有空窗。

── 频率设计（2026-11-17 22:0x 实测标定）────────────────────────────
每轮先做一次窗口枚举（实测 0.0001s，基本免费）：
  ① 出现「小窗」（宽<700 或 高<620：红包/首充这类独立弹窗外框）
     → 立刻完整识别（延迟 ≤ 1 个 interval）
  ② 没有小窗 → 每 FULL_EVERY 秒完整识别一次（覆盖主窗里的浮层广告）
  ③ 完整识别之间至少有 MIN_FULL_GAP 秒间隔，小窗一直关不掉时也不会烧 CPU

完整的「扫描广告」只在**真的有平台窗口**时才做：
  ad_guard 会拿每个窗口画面做 19 档多尺度模板匹配，实测 1712x934 的
  War3 游戏窗（标题 wyplatform_war3_game）一次要吃 1.29 秒——而游戏里根本
  不会有平台首充/红包广告。所以这里先看有没有标题含「对战平台」的窗口，
  没有就跳过 ad_guard，整轮降到 0.001s 级（原先平均占用 13% 一个核心）。

用法：
  python popup_watch.py                # 前台常驻（Ctrl+C 停）
  python popup_watch.py --minutes 240  # 最长跑 240 分钟
  python popup_watch.py --interval 1.0 # 枚举节奏（默认 0.8s）
  python popup_watch.py --full-every 180 # 空转兜底间隔（默认 180s = 3 分钟）
  python popup_watch.py --once         # 只扫一次就退出（调试）
  python popup_watch.py --probe        # 只测各环节耗时，不点任何东西
  python popup_watch.py --status       # 看现在有没有在跑
  python popup_watch.py --stop         # 停掉在跑的那个

由 full_loop 的 A 段以「脱离父进程」方式拉起；单实例靠命名互斥体保证，
同时只会有 1 个在跑。关闭记录写在 logs/popup_events.jsonl，
本进程流水写在 logs/popup_watch.log（每 60 秒一条心跳，带真实周期/占用）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

LOGS = os.path.join(HERE, "logs")
PID_FILE = os.path.join(LOGS, "popup_watch.pid")
LOG_FILE = os.path.join(LOGS, "popup_watch.log")
IDLE_EXIT_SEC = 180.0        # 连续这么久看不到平台窗口 → 自己退出
FULL_EVERY = 180.0           # 空转兜底间隔（3分钟）：窗口一有变化就立刻识别（--full-every 可改）
MIN_FULL_GAP = 1.0           # 两次完整识别的最小间隔（小窗关不掉时防烧 CPU）
MAX_SMALL_GAP = 15.0         # 小窗关不掉时退避上限：1s->2->4->8->15s（不让它把核心跑满）
HEARTBEAT_SEC = 60.0         # 心跳日志间隔：报告真实周期与占用
MUTEX_NAME = "Local\\KKAutoPopupWatch"
STOP_EVENT = "Local\\KKAutoPopupWatchStop"
_STOP_K = _STOP_H = None


def _say(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(LOGS, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _mutex_dup():
    """已经有别的实例持着这个互斥体吗？"""
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW(None, False, MUTEX_NAME)
        return ctypes.get_last_error() == 183
    except Exception:
        return False


def _pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong(0)
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259
    except Exception:
        return False


def running_pid():
    try:
        pid = int(open(PID_FILE, encoding="utf-8").read().strip())
    except Exception:
        return 0
    return pid if _pid_alive(pid) else 0


def _stop_handle(create=False):
    """命名事件：停监控不靠 pid（pid 文件可能写不进去）"""
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        if create:
            h = k.CreateEventW(None, True, False, STOP_EVENT)
            return k, h
        return k, k.OpenEventW(0x0002, False, STOP_EVENT)   # EVENT_MODIFY_STATE
    except Exception:
        return None, None


def request_stop():
    """给常驻监控发退出信号；没有实例在跑返回 False"""
    k, h = _stop_handle(False)
    if not h:
        return False
    try:
        k.SetEvent(h)
        k.CloseHandle(h)
        return True
    except Exception:
        return False


def stop_requested():
    if not _STOP_H:
        return False
    try:
        return _STOP_K.WaitForSingleObject(_STOP_H, 0) == 0
    except Exception:
        return False


def is_running():
    """在跑返回 pid（拿不到 pid 但有互斥体时返回 -1），没跑返回 0"""
    pid = running_pid()
    if pid:
        return pid
    return -1 if _mutex_dup() else 0




def _write_pid():
    data = str(os.getpid())
    for _ in range(2):
        try:
            os.makedirs(LOGS, exist_ok=True)
            with open(PID_FILE, "w", encoding="utf-8") as f:
                f.write(data)
            return True
        except Exception:
            try:
                if os.path.exists(PID_FILE):
                    os.remove(PID_FILE)
            except Exception:
                return False
    return False


def _clear_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def platform_windows():
    try:
        import popup_guard
        return popup_guard.enum_candidates(include_war3=False)
    except Exception:
        return []


def _is_small(size):
    w, h = int(size[0]), int(size[1])
    return w < 700 or h < 620


def ad_host_ready(wins=None):
    """有没有「可能挂平台广告」的窗口？

    首充/红包这类广告只出现在标题含「对战平台」的窗口（主窗 + 那个 415x448
    独立弹窗窗）。War3 游戏窗（wyplatform_war3_game）不算——它只会让
    ad_guard 白跑 1.29 秒。
    """
    if wins is None:
        wins = platform_windows()
    for item in wins:
        title = (item[1] or "")
        size = item[2]
        if "对战平台" in title:
            return True
        # 兜底：明显不是游戏的大窗也算（防止平台改标题后漏扫）
        try:
            if int(size[0]) >= 900 and "war3" not in title.lower():
                return True
        except Exception:
            pass
    return False


def scan_and_close(quiet=True, wins=None):
    """完整识别一遍。返回（关掉数量, 说明列表）

    广告扫描（ad_guard）只在有平台窗口时才做，见 ad_host_ready() 的说明。
    """
    n = 0
    msgs = []
    try:
        import popup_guard
        k = popup_guard.close_popups(max_n=3, quiet=quiet)
        if k:
            n += k
            msgs.append("活动弹窗")
    except Exception:
        pass
    if ad_host_ready(wins):
        try:
            import ad_guard
            if ad_guard.close(verbose=False) == "closed":
                n += 1
                msgs.append("首充/活动广告")
        except Exception:
            pass
    return n, msgs


def probe(interval=0.8, full_every=None):
    """只测耗时，不点任何东西——用来核对频率设定是否合理"""
    if full_every is None:
        full_every = FULL_EVERY
    import popup_guard as pg
    try:
        pg.enable_dpi_awareness()
    except Exception:
        pass

    def avg(fn, n=3):
        ts = []
        for _ in range(n):
            t0 = time.time()
            try:
                fn()
            except Exception:
                pass
            ts.append(time.time() - t0)
        return sum(ts) / max(1, len(ts))

    wins = platform_windows()
    t_enum = avg(lambda: pg.enum_candidates(include_war3=False), 6)
    t_pg = avg(lambda: pg.close_popups(max_n=3, quiet=True), 3)
    has_host = ad_host_ready(wins)
    t_ad = 0.0
    if has_host:
        import ad_guard
        t_ad = avg(lambda: ad_guard.close(verbose=False), 3)
    print("当前平台窗口：%s" % ([(w[1], w[2]) for w in wins] or "无"))
    print("窗口枚举              %.4fs" % t_enum)
    print("popup_guard 完整识别   %.4fs" % t_pg)
    print("ad_guard    完整识别   %.4fs   %s"
          % (t_ad, "(有平台窗口，会执行)" if has_host else "(无平台窗口 -> 跳过，省约 1.3s)"))
    full = t_enum + t_pg + t_ad
    print("")
    print("静止轮：枚举 %.4fs + 停顿 %.2fs  -> 周期 %.2fs，每秒 %.1f 次"
          % (t_enum, interval, t_enum + interval, 1.0 / max(0.01, t_enum + interval)))
    print("完整轮：%.4fs，每 %.1fs 一次    -> 平均占用 %.2f%% 一个核心"
          % (full, full_every, 100.0 * full / max(0.1, full_every)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="平台弹窗常驻监控")
    ap.add_argument("--minutes", type=float, default=240.0, help="最长运行分钟数")
    ap.add_argument("--interval", type=float, default=0.8,
                    help="窗口枚举间隔秒（默认 0.8；越小发现越快、越费 CPU）")
    ap.add_argument("--full-every", type=float, default=FULL_EVERY,
                    help="没有小窗时完整识别间隔秒（默认 180）")
    ap.add_argument("--once", action="store_true", help="只扫一次")
    ap.add_argument("--probe", action="store_true", help="只测耗时，不点东西")
    ap.add_argument("--status", action="store_true", help="查询是否在跑")
    ap.add_argument("--stop", action="store_true", help="停掉在跑的那个")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.probe:
        return probe(max(0.2, args.interval), max(MIN_FULL_GAP, args.full_every))

    if args.status:
        pid = running_pid() or is_running()
        if pid > 0:
            print("常驻监控在跑：pid=%d" % pid)
        elif pid < 0:
            print("常驻监控在跑（pid 未知）")
        else:
            print("常驻监控没在跑")
        return 0

    if args.stop:
        if request_stop():
            print("已通知常驻监控退出（事件信号）")
            return 0
        pid = running_pid()
        if not pid:
            print("没有在跑的监控（或拿不到 pid）")
            return 0
        try:
            import subprocess
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, creationflags=0x08000000)
            print("已停止 pid=%d" % pid)
        except Exception as e:
            print("停止失败：%r" % (e,))
        return 0

    try:
        import popup_guard
        popup_guard.enable_dpi_awareness()
    except Exception:
        pass

    if args.once:
        n, msgs = scan_and_close(quiet=not args.verbose)
        print("扫一次：关掉 %d 个 %s" % (n, "、".join(msgs) if msgs else ""))
        return 0

    if _mutex_dup():
        _say("已有实例在跑（互斥体），本次退出")
        return 0

    interval = max(0.2, float(args.interval))
    full_every = max(MIN_FULL_GAP, float(args.full_every))

    global _STOP_K, _STOP_H
    _STOP_K, _STOP_H = _stop_handle(True)
    try:
        _STOP_K.ResetEvent(_STOP_H)     # 清掉上次残留的信号
    except Exception:
        pass
    _write_pid()
    _say("弹窗常驻监控启动 pid=%d 枚举=%.2fs 完整识别=%.0fs 上限=%.0f 分钟"
         % (os.getpid(), interval, full_every, max(0.05, args.minutes)))
    t_end = time.time() + max(0.05, args.minutes) * 60
    idle_since = None
    last_full = 0.0
    sig_prev = None           # 上一轮平台窗口签名（hwnd+尺寸）
    rounds = 0
    fulls = 0
    closed_total = 0
    work_total = 0.0          # 累计真正干活的时间（用于心跳报告占用）
    hb_t = time.time()
    hb_rounds = 0
    hb_work = 0.0
    fail_streak = 0           # 小窗一直在又关不掉 -> 退避等级
    hb_fulls = 0              # 本心跳周期内完整识别次数
    try:
        while time.time() < t_end:
            if stop_requested():
                _say("收到停止信号，退出")
                break
            t_round = time.time()
            rounds += 1
            hb_rounds += 1
            wins = platform_windows()
            small = [w for w in wins if _is_small(w[2])]
            now = time.time()
            sig = tuple((w[0], w[2]) for w in wins)
            changed = (sig != sig_prev)      # 有窗口新开/消失/改尺寸
            sig_prev = sig
            # 退避：小窗一直在却关不掉（典型是未登记的「平台提示」480x300），
            # 每轮完整识别约 2s，1s 一次的节奏能把一个核心跑满 -> 逐级放慢到 15s
            small_gap = min(MAX_SMALL_GAP, MIN_FULL_GAP * (2 ** min(fail_streak, 4)))
            due_small = bool(small) and (now - last_full >= small_gap)
            due_change = changed and (now - last_full >= MIN_FULL_GAP)
            due_full = (now - last_full >= full_every)
            if due_small or due_change or due_full:
                n, msgs = scan_and_close(quiet=not args.verbose, wins=wins)
                last_full = time.time()
                fulls += 1
                hb_fulls += 1
                if n:
                    closed_total += n
                    fail_streak = 0
                    _say("🧹 关掉 %d 个弹窗（%s）累计 %d"
                         % (n, "、".join(msgs) or "已登记弹窗", closed_total))
                elif small and not changed:
                    fail_streak += 1
                else:
                    fail_streak = 0
            if wins:
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since > IDLE_EXIT_SEC:
                    _say("连续 %.0fs 没有平台窗口 → 自动退出" % IDLE_EXIT_SEC)
                    break
            spent = time.time() - t_round
            work_total += spent
            hb_work += spent
            # 周期补偿：让「枚举 + 停顿」稳定在 interval，不被扫描耗时拖长
            time.sleep(max(0.05, interval - spent))
            if time.time() - hb_t >= HEARTBEAT_SEC:
                el = time.time() - hb_t
                _say("💓 心跳 %.0fs：%d 轮（每轮 %.2fs）完整识别 %d 次 退避 %d 级 "
                     "占用 %.1f%% 已关弹窗 %d"
                     % (el, hb_rounds, el / max(1, hb_rounds), hb_fulls,
                        min(fail_streak, 4),
                        100.0 * hb_work / max(0.1, el), closed_total))
                hb_t = time.time()
                hb_rounds = 0
                hb_work = 0.0
                hb_fulls = 0
    except KeyboardInterrupt:
        _say("收到 Ctrl+C，退出")
    finally:
        _clear_pid()
    _say("常驻监控结束：扫 %d 轮（完整识别 %d 次），共关掉 %d 个弹窗"
         % (rounds, fulls, closed_total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
