# -*- coding: utf-8 -*-
"""短时真机实测：复用房间、执行开局、等 War3 窗口；不进入 15 分钟大厅等待。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "logs", "live_check.json")
MAX_WAIT = 60


def _war3_windows(ga):
    result = []
    for w in ga.enum_windows_full():
        exe = (w.get("exe") or "").lower()
        title = (w.get("title") or "").lower()
        if ("war3" in exe or "warcraft iii" in exe or "warcraft" in title) \
                and w.get("w", 0) >= 400 and w.get("h", 0) >= 400:
            result.append({"hwnd": w["hwnd"], "exe": w.get("exe"),
                           "title": w.get("title"), "width": w["w"], "height": w["h"]})
    return result


def run():
    started = time.time()
    report = {"status": "error", "max_wait_seconds": MAX_WAIT}
    try:
        import room_finder as rf
        import game_auto as ga

        if _war3_windows(ga):
            report["status"] = "war3_already_running"
        else:
            windows = rf.platform_windows(min_client=300)
            large = [w for w in windows if 1100 <= w["client"][2] <= 1350
                     and 820 <= w["client"][3] <= 980]
            room = rf.find_room(verbose=False, restore=True)
            if room is None:
                room = ga._win7_room_pair(large)
            if room is None:
                report["status"] = "room_not_confirmed"
            elif rf.is_room_busy(room):
                report["room_hwnd"] = room["hwnd"]
                import plat_restart as pr
                restart = pr.restart(verbose=True, force=True)
                report["restart_result"] = restart.get("status")
                report["status"] = ("platform_restarted" if restart.get("status") == "ok"
                                    else "platform_restart_failed")
            else:
                report["room_hwnd"] = room["hwnd"]
                command = [sys.executable, os.path.join(HERE, "game_auto.py"),
                           "pipeline", "--pipeline", os.path.join(HERE, "configs", "pipeline.json"),
                           "--only", "④"]
                report["start_step"] = "④ room start only"
                try:
                    result = subprocess.run(command, cwd=HERE, timeout=45)
                    report["start_exit"] = result.returncode
                except subprocess.TimeoutExpired:
                    report["status"] = "start_step_timeout"
                if "status" not in report or report["status"] == "error":
                    deadline = time.monotonic() + MAX_WAIT
                    while time.monotonic() < deadline:
                        seen = _war3_windows(ga)
                        if seen:
                            report["status"] = "war3_started"
                            report["war3_windows"] = seen
                            break
                        if report.get("start_exit") != 0:
                            report["status"] = "start_step_failed"
                            break
                        time.sleep(1)
                    else:
                        report["status"] = "war3_not_seen_in_60s"
    except Exception:
        report["error"] = traceback.format_exc(limit=5)
    report["elapsed_seconds"] = round(time.time() - started, 2)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    try:
        print(json.dumps({"status": report["status"], "elapsed_seconds": report["elapsed_seconds"],
                          "out": OUT}, ensure_ascii=False), flush=True)
    except Exception:
        pass
    return 0 if report["status"] in ("war3_started", "platform_restarted") else 1


if __name__ == "__main__":
    sys.exit(run())
