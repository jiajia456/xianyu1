"""桩测试：单局超时（2026-11-17 15:20 起）只杀 War3 重开游戏，**不重启平台**。

做法：把阶段函数全换成瞬间返回 True 的桩，用假时钟让时间飞快跑过 50 分钟，
看主循环命中 ROUND_HIT 后走的是「kill_war3_and_wait → 重跑本轮」还是旧的「restart_platform」。
"""
import io
import sys
import contextlib
import time as _real_time

import full_loop as fl

# ---------------------------------------------------------------- 假时钟
class FakeTime:
    """每次 time() 前进 STEP 秒，sleep 直接跳过，其余属性照旧用真 time"""

    STEP = 120

    def __init__(self):
        self.t = 1_700_000_000.0

    def time(self):
        self.t += self.STEP
        return self.t

    def sleep(self, _s):
        pass

    def strftime(self, fmt=None, t=None):
        return "00:00:00"

    def __getattr__(self, k):
        return getattr(_real_time, k)


class StopTest(Exception):
    pass


restarts = []
kills = []

# ---------------------------------------------------------------- 桩
def fake_stage(cfg, rec, args):
    return True


def fake_kill(verbose=True):
    kills.append(len(kills) + 1)
    if len(kills) >= 3:              # 连续第 3 次超时 → 结束测试
        raise StopTest()
    return True


def fake_restart(why="", verbose=True):
    restarts.append(why)
    return True, "ok"


def main():
    fl.time = FakeTime()
    fl.STAGE_FN = {k: fake_stage for k in fl.STAGE_FN}
    fl.check_login = lambda label="": False
    fl.disc_check = lambda label="": False
    fl.login_watch_start = lambda: None
    fl.park_mouse = lambda verbose=True: None
    fl.kill_war3_and_wait = fake_kill
    fl.restart_platform = fake_restart
    fl.LOGIN["ts"] = None
    fl.RISK_HIT["v"] = False
    fl.BACK_HIT["v"] = False
    fl.BUSY_HIT["v"] = False
    fl.ROUND_HIT["v"] = False
    print(f"ROUND limit = {fl.ROUND['limit']} 分钟（假时钟每步 +{FakeTime.STEP}s）")

    extra = sys.argv[1:]                  # 允许外部追加参数（如 --risk-retry 0）
    sys.argv = ["full_loop.py", "--rounds", "1", "--config", "configs/full_loop.json",
                "--max-round-min", "1"] + extra
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fl.main()
    except StopTest:
        pass
    except SystemExit:
        pass
    return buf.getvalue()


out = main()
lines = out.splitlines()
hit = [l for l in lines if "单局" in l or "重开游戏" in l or "War3 进程" in l
       or "重新开始游戏" in l]
print("\n".join(hit[:20]))
print("\n--- 判定 ---")
print(f"restart_platform 调用次数 = {len(restarts)}  {'✅ 没重启平台' if not restarts else '❌ 还在重启平台'}")
print(f"kill_war3_and_wait 次数 = {len(kills)}  {'✅ 走的是杀 War3 重开' if kills else '❌ 没走重开'}")
print(f"命中「单局超时」提示 = {'✅' if any('单局已超' in l or '单局超 ' in l for l in hit) else '❌'}")
print(f"出现「重启平台」字样 = {'❌ 有（见下）' if any('重启平台' in l and '不重启' not in l for l in hit) else '✅ 无'}")
print(f"出现「重启平台」字样的行 = {[l for l in hit if '重启平台' in l and '不重启' not in l]}")
