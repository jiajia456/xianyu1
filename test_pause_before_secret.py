"""桩测试：D 存档挑战 → E 秘境挑战之间的等待（2026-11-17 16:13 用户要求 30s）。

高难度 N8+：D 打完等 30s 再开 E
低难度 N1~N7：E 跳过，改成等 60s 再走收尾（原有规则，一起回归）
"""
import io
import sys
import contextlib
import time as _real_time

import full_loop as fl


class Clock:
    def __init__(self):
        self.sleeps = 0

    def time(self):
        return _real_time.time()

    def sleep(self, _s):
        self.sleeps += 1

    def strftime(self, *a):
        return _real_time.strftime(*a)

    def __getattr__(self, k):
        return getattr(_real_time, k)


def run_case(title, diff):
    order = []
    waits = []          # 记录每次 wait_quiet 申请的秒数（不再数 sleep 次数）

    def mk(k):
        def f(cfg, rec, args):
            order.append((k, len(waits)))
            return True
        return f

    orig = (fl.STAGE_FN, fl.sh, fl.check_login,
            fl.login_watch_start, fl.park_mouse, fl.wait_quiet)
    fl.STAGE_FN = {k: mk(k) for k in fl.STAGE_FN}
    fl.sh = lambda cmd, label, timeout=1200: (0, "")
    fl.check_login = lambda label="": False
    fl.login_watch_start = lambda: None
    fl.park_mouse = lambda verbose=True: None
    # 真正的等待改成「只记账、不真等」（wait_quiet 按截止时间算，假 sleep 会变忙等）
    fl.wait_quiet = lambda s, why="", verbose=True: (waits.append(int(s)), False)[1]
    fl.LOGIN["ts"] = None
    fl.RISK_HIT["v"] = False
    fl.BACK_HIT["v"] = False
    fl.BUSY_HIT["v"] = False
    fl.ROUND_HIT["v"] = False

    sys.argv = ["full_loop.py", "--rounds", "1", "--config", "configs/full_loop.json",
                "--difficulty", diff]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fl.main()
    except SystemExit:
        pass
    (fl.STAGE_FN, fl.sh, fl.check_login,
     fl.login_watch_start, fl.park_mouse, fl.wait_quiet) = orig

    seq = [k for k, _ in order]
    print("\n### %s（难度 %s）阶段顺序：%s" % (title, diff, " → ".join(seq)))
    if "slots" in seq:
        i = seq.index("slots")
        n_after = len(waits) - order[i][1]
        secs = sum(waits[order[i][1]:])
        nxt = seq[i + 1] if i + 1 < len(seq) else "(结束)"
        print("    D 之后的等待 = %d 次，共 %d 秒（下一阶段：%s）" % (n_after, secs, nxt))
    new = [l for l in buf.getvalue().splitlines() if "存档挑战已完成" in l]
    for l in new:
        print("   ", l.strip())
    return seq


run_case("高难度：D → E 之间", "N8")
run_case("低难度：E 跳过，D → 收尾 之间", "N3")
