# -*- coding: utf-8 -*-
"""几秒内检查系统、依赖与平台双窗口识别；不进入游戏、不点击。"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "logs", "quick_check.json")


def run():
    start = time.time()
    report = {"status": "error", "seconds": 0, "windows": []}
    try:
        import sysenv
        import room_finder as rf
        import game_auto as ga

        report["system"] = sysenv.info(refresh=True)
        windows = rf.platform_windows(min_client=300)
        candidates = [w for w in windows
                      if 1100 <= w["client"][2] <= 1350
                      and 820 <= w["client"][3] <= 980]
        pair = ga._win7_room_pair(candidates)
        for w in windows:
            row = {"hwnd": w["hwnd"], "pid": w["pid"], "title": w["title"],
                   "width": w["client"][2], "height": w["client"][3],
                   "minimized": w["iconic"]}
            if not w["iconic"] and w in candidates:
                scores = rf.peek_window(w["hwnd"])
                if scores is not None:
                    row["room_no_score"], row["start_score"], row["create_score"] = [
                        round(float(x), 3) for x in scores]
            report["windows"].append(row)

        if not windows:
            report["status"] = "platform_not_found"
        elif pair:
            found = rf.find_room(verbose=False, restore=False)
            report["status"] = "room_busy" if rf.is_room_busy(found) else "room_pair_found"
            report["room_hwnd"] = pair["hwnd"]
            if report["status"] == "room_busy":
                report["next_step"] = "restart_all_kk_platform_processes"
            else:
                report["next_step"] = "reuse_room_step_4"
        elif len(candidates) >= 2:
            report["status"] = "ambiguous_windows"
        else:
            report["status"] = "no_room_pair"
            report["next_step"] = "create_room"
    except Exception:
        report["error"] = traceback.format_exc(limit=5)
    report["seconds"] = round(time.time() - start, 2)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    try:
        print(json.dumps({"status": report["status"], "seconds": report["seconds"],
                          "out": OUT}, ensure_ascii=False), flush=True)
    except Exception:
        pass
    return 0 if report["status"] in ("room_pair_found", "room_busy", "no_room_pair") else 1


if __name__ == "__main__":
    sys.exit(run())
