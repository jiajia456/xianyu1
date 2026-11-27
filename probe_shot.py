# -*- coding: utf-8 -*-
"""抓一张现场图（War3 / 平台 / 全屏），用于调试和做模板。

用法：
  python probe_shot.py                 # War3 窗口 → %TEMP%/kkfish/shot_war3.png
  python probe_shot.py --plat          # 平台窗口
  python probe_shot.py --out D:/a.png  # 指定输出
"""
from __future__ import annotations

import argparse
import os
import time

import cv2

import popup_guard as pg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plat", action="store_true", help="抓平台窗口（默认抓 War3）")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    pg.enable_dpi_awareness()
    out = a.out or os.path.join(os.environ.get("TEMP", "."), "kkfish",
                                "shot_plat.png" if a.plat else "shot_war3.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    if a.plat:
        cands = pg.enum_candidates(False)
        if not cands:
            print("❌ 没有平台窗口")
            return 3
        hwnd, title, size = cands[0]
    else:
        import game_auto as ga
        w = ga.GameWindow(exe=["War3.exe"], title_re=[r"Warcraft"], pick="largest")
        hwnd, title, size = w.hwnd, "War3", w.client_rect()[2:]

    img = pg.grab_hwnd(hwnd)
    if img is None or not img.size:
        print("❌ 抓不到画面 hwnd=%s" % hwnd)
        return 4
    ok = cv2.imwrite(out, img)
    print("hwnd=%s title=%r size=%s shape=%s → %s (write=%s)"
          % (hwnd, title, size, img.shape, out, ok))
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
