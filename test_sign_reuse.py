# -*- coding: utf-8 -*-
"""自检：签到降 CPU 的两处改动

1) plat_home.face_of 详情页判据前置 —— 分类结果必须和以前一致，只是详情页更快
2) daily_sign --detail-hwnd 复用父进程确认过的详情页 —— 省掉重复的全窗扫描

设计原则：全部用「真机窗口」做判据（没有窗口就明确跳过，不假通过），
--check 模式下 daily_sign 只算位置不点击，所以可以放心真跑。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import popup_guard as pg          # noqa: E402
import plat_home as ph            # noqa: E402
import daily_sign as ds           # noqa: E402

PASS, FAIL, SKIP = [], [], []


def ck(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def skip(name, why):
    SKIP.append(name)
    print(f"  ⏭  {name}（{why}）")


def windows():
    """{hwnd: img} —— 抓当前所有平台窗"""
    out = {}
    for hwnd, _title, _size in pg.enum_candidates(False):
        img = pg.grab_hwnd(hwnd)
        if img is not None:
            out[hwnd] = img
    return out


def main():
    print("=" * 60)
    print("① face_of：详情页判据前置，分类结果不变")
    print("=" * 60)
    wins = windows()
    if not wins:
        skip("face_of 全部用例", "没有平台窗口")
        faces = {}
    else:
        faces = {}
        cost = {}
        for hwnd, img in wins.items():
            t = time.process_time()
            f, info = ph.face_of(img)
            cost[hwnd] = time.process_time() - t
            faces[hwnd] = (f, info)
            print(f"     hwnd={hwnd} {img.shape[1]}x{img.shape[0]} → {f} {info}  {cost[hwnd]:.3f}s")

        det = [h for h, (f, _) in faces.items() if f == "detail"]
        room = [h for h, (f, _) in faces.items() if f == "room"]

        if det:
            h = det[0]
            f, info = faces[h]
            ck("详情页仍判为 detail", f == "detail", f)
            ck("详情页不再带 roomno/start（证明 is_room 没跑）",
               "roomno" not in info, str(info))
            ck("详情页 create 分仍 ≥0.85", info.get("create", 0) >= 0.85, str(info))
            if room:
                ck("详情页比房间窗更快（早退生效）",
                   cost[h] < cost[room[0]], f"{cost[h]:.3f} vs {cost[room[0]]:.3f}")
        else:
            skip("详情页分类用例", "当前没有详情页窗口")

        if room:
            h = room[0]
            f, info = faces[h]
            ck("房间窗仍判为 room", f == "room", f)
            ck("房间窗仍带 roomno/start（房间判据没被破坏）",
               "roomno" in info and "start" in info, str(info))
            ck("房间窗 create 分远低于详情页阈值（0.85）",
               info.get("create", 1) < 0.85, str(info))
        else:
            skip("房间窗分类用例", "当前没有房间窗口")

    print()
    print("=" * 60)
    print("② _reuse_detail：只认「还在 + 可见 + 客户区够大」")
    print("=" * 60)
    ck("0 → None", ds._reuse_detail(0) is None)
    ck("无效句柄 → None", ds._reuse_detail(0x7FFFFFF) is None)
    det = [h for h, (f, _) in faces.items() if f == "detail"] if faces else []
    if det:
        ck("详情页 hwnd 原样返回", ds._reuse_detail(det[0]) == det[0])
    else:
        skip("详情页 hwnd 原样返回", "当前没有详情页窗口")

    tiny = None
    try:
        import win32gui
        for hwnd, _t, size in pg.enum_candidates(False):
            if size[0] < 1000 or size[1] < 700:
                tiny = hwnd
                break
        if tiny:
            ck("小窗口（<1000x700）被拒", ds._reuse_detail(tiny) is None, str(tiny))
        else:
            skip("小窗口被拒", "当前没有小尺寸平台窗")
    except Exception as e:
        skip("小窗口被拒", repr(e))

    print()
    print("=" * 60)
    print("③ daily_sign --check：复用路径 / 老路径都能跑（只算位置，不点击）")
    print("=" * 60)
    if not det:
        skip("daily_sign 复用路径", "当前没有详情页窗口")
        skip("daily_sign 老路径", "当前没有详情页窗口")
    else:
        hwnd = det[0]
        base = [sys.executable, os.path.join(HERE, "daily_sign.py"), "--check"]
        r1 = subprocess.run(base + ["--detail-hwnd", str(hwnd)],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", cwd=HERE)
        out1 = (r1.stdout or "") + (r1.stderr or "")
        ck("复用路径退出码 0", r1.returncode == 0, f"rc={r1.returncode} {out1[-200:]}")
        ck("复用路径打印了「跳过重复的形态扫描」", "跳过重复的形态扫描" in out1, out1[-200:])
        ck("复用路径确实复用了窗口", "复用详情页窗口" in out1, out1[-200:])
        ck("复用路径仍能定位「签到」按钮", "签到按钮" in out1, out1[-200:])

        r2 = subprocess.run(base, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=HERE)
        out2 = (r2.stdout or "") + (r2.stderr or "")
        ck("老路径（不带 hwnd）退出码 0", r2.returncode == 0, f"rc={r2.returncode}")
        ck("老路径仍走 ensure_detail", "平台形态" in out2 or "ensure_detail" in out2 or "详情页" in out2,
           out2[-200:])

    print()
    print("=" * 60)
    print(f"通过 {len(PASS)} ｜ 失败 {len(FAIL)} ｜ 跳过 {len(SKIP)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    print("全部通过" + ("（部分用例因环境跳过）" if SKIP else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
