# -*- coding: utf-8 -*-
"""建房 + 设密码 3 轮循环实测（不开始游戏）。

每轮：①②③（搜图 → 建房 → 填密码并提交）→ 核实房间名/密码 → 退房。
退房成功才进下一轮；三轮都通过才算好。

跑法：python test_room_pwd_loop.py [轮数]
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def log(msg):
    print(msg, flush=True)


def run_build():
    """只跑 ①②③：搜图 → 建房 → 填密码并提交（不点开始游戏）。"""
    cmd = [sys.executable, "-W", "ignore", "game_auto.py", "pipeline",
           "--pipeline", "configs/pipeline.json", "--only", "①,②,③"]
    p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    path = os.path.join(LOG_DIR, "loop_build_%s.log" % time.strftime("%H%M%S"))
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(out)
    return p.returncode, out, path


def check_room():
    """核实房间名「我在峡谷打」+ 密码已设置（白点/圆点）。"""
    import room_finder as rf
    try:
        room = rf.find_room(verbose=False, restore=True)
    except Exception as e:
        return {"err": repr(e)}
    if not room:
        return None
    try:
        _h, info = rf.check_room(room["hwnd"], verbose=False)
    except Exception as e:
        return {"err": repr(e), "hwnd": room["hwnd"]}
    if info is None:
        return {"hwnd": room["hwnd"], "readable": False}
    return {
        "hwnd": room["hwnd"],
        "readable": True,
        "name_ok": bool(info["name_ok"]),
        "pwd_ok": bool(info["pwd_ok"]),
        "name_score": float(info["name_score"]),
        "pwd_white": info.get("pwd_white"),
        "pwd_dots": info.get("pwd_dots"),
    }


def leave_room():
    import room_finder as rf
    try:
        room = rf.find_room(verbose=False)
    except Exception as e:
        return "find_err:%r" % (e,)
    if not room:
        return "no_room"
    try:
        return rf.leave_room(room["hwnd"], verbose=False)
    except Exception as e:
        return "leave_err:%r" % (e,)


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    log("=" * 62)
    log("建房 + 设密码 循环测试：%d 轮（不开始游戏）" % ROUNDS)
    log("=" * 62)
    ok_n = 0
    rows = []
    for i in range(1, ROUNDS + 1):
        log("")
        log("---------------- 第 %d/%d 轮 ----------------" % (i, ROUNDS))
        t0 = time.time()
        rc, out, path = run_build()
        cost = time.time() - t0
        # 从日志里摘关键行
        keys = []
        for line in out.splitlines():
            if any(k in line for k in ("输入生效", "密码框有内容", "补输", "取消",
                                       "点「创建」", "建房子弹窗", "房间号=", "失败",
                                       "全链路完成")):
                keys.append(line.rstrip())
        for line in keys[-6:]:
            log("   " + line.strip())

        chk = check_room()
        if chk is None:
            log("   ❌ 第 %d 轮：建完房找不到房间窗（rc=%s）" % (i, rc))
            rows.append((i, "NO_ROOM", "-", "-", "-"))
            break
        if chk.get("err"):
            log("   ❌ 第 %d 轮：核实出错 %s" % (i, chk["err"]))
            rows.append((i, "ERR", "-", "-", "-"))
        elif not chk.get("readable"):
            log("   ⚠️ 第 %d 轮：房间画面读不出来（hwnd=%s）" % (i, chk.get("hwnd")))
            rows.append((i, "UNREADABLE", "-", "-", "-"))
        else:
            good = chk["name_ok"] and chk["pwd_ok"]
            log("   %s 第 %d 轮核实：房间名 %s（%.3f） | 密码 %s"
                % ("✅" if good else "❌", i,
                   "✔ 我在峡谷打" if chk["name_ok"] else "✘ 名字不对",
                   chk["name_score"],
                   ("✔ 已设置（白点 %s/%s 个）" % (chk["pwd_white"], chk["pwd_dots"]))
                   if chk["pwd_ok"] else "✘ 未设置"))
            rows.append((i, "OK" if good else "BAD", chk["name_score"],
                         chk["pwd_white"], chk["pwd_dots"]))
            if good:
                ok_n += 1

        lr = leave_room()
        log("   退房：%s" % lr)
        if not (lr is True or lr in ("clicked", "no_room", "left")):
            log("   ⚠️ 退房没成功（%s）→ 停止循环，避免下一轮撞「您正在房间内」" % lr)
            break
        time.sleep(1.5)

    log("")
    log("=" * 62)
    log("结果：%d/%d 轮建房+密码都正确" % (ok_n, ROUNDS))
    for r in rows:
        log("  第 %s 轮: %s | 房间名分=%s | 白点=%s | 圆点=%s" % r)
    log("=" * 62)
    return 0 if ok_n == ROUNDS else 1


if __name__ == "__main__":
    raise SystemExit(main())
