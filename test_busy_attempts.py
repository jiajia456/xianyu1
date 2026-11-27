"""桩测试：无法创建房间时「整段搜图→建房」尝试 4 次、每次间隔 6 分钟，4 次都不行才重启平台。"""
import full_loop as fl
import loop_round as lr


class Args:
    skip_upgrade = False


def run_case(name, script):
    calls = []
    idx = [0]

    def fake_sh(cmd, label, timeout=1200):
        rc = script[min(idx[0], len(script) - 1)]
        idx[0] += 1
        busy_arg = ""
        if "--busy-wait" in cmd:
            busy_arg = cmd[cmd.index("--busy-wait") + 1]
        calls.append((label, "rc=%d" % rc, "busy-wait=%s" % busy_arg, "timeout=%ds" % timeout))
        return rc, ""

    orig = (fl.sh, fl.restart_platform, lr.wait_ingame)
    fl.sh = fake_sh
    fl.restart_platform = lambda why="", verbose=True: (calls.append(("RESTART", why)), (True, "ok"))[1]
    lr.wait_ingame = lambda timeout=150, tag="": True
    fl.RISK_HIT["v"] = False
    fl.LOGIN["ts"] = None
    fl.ROUND_HIT["v"] = False
    fl.BUSY_HIT["v"] = False
    rec = {}
    cfg = {"busy_wait_minutes": 6, "busy_attempts": 4, "restart_on_enter_fail": True,
           "timeout_in_game": 150, "cleanup": False}
    ok = fl.stage_enter(cfg, rec, Args())
    fl.sh, fl.restart_platform, lr.wait_ingame = orig
    print("\n### %s  ok=%s  BUSY_HIT=%s" % (name, ok, fl.BUSY_HIT["v"]))
    print("    rec =", rec)
    for c in calls:
        print("    ", c)
    return calls


run_case("一直 exit=13（僵尸会话）：试 4 次 → 重启 → 再 4 次 → 放弃", [13])
run_case("第 3 次才成功", [13, 13, 0])
run_case("exit=11 建房子弹窗不出来", [11])
run_case("exit=5 其它失败（不重启）", [5])
run_case("第一次就成功", [0])
