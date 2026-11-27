# -*- coding: utf-8 -*-
"""地图更新守卫 —— 「平台提示 / 地图已更新，请重新下载地图。」

场景（用户 2026-11-18 现场）
--------------------------
平台把地图推到新版本（例：v1.0.46 → v1.0.47）之后：
1. 退出游戏 / 点开局时，平台弹一个 480x300 的「平台提示」小窗：
   **「地图已更新，请重新下载地图」** + 一个蓝色「确定」。
2. **点「确定」只是把这个提示关掉，地图根本没有下载。** 平台退回地图详情页，
   右上角版本号已经是新版，但没有房间、没有进度条。
3. 此时直接点「开始游戏」会 War3 秒退（或点了没反应），因为本地地图还是旧版。
4. 真正触发重新下载的动作是：详情页点「创建房间」→ 填密码 → 点「创建」。
   平台在建房的同时把地图拉下来；**房间窗口（1224x904）出现 = 下载完成**（不看进度条）。

本模块按用户要求把上面这套做成「以后遇见直接处理」：

    find()          只查弹窗（返回 dict / None）
    dismiss()       点「确定」关掉它
    rebuild_room()  建房重下：创建房间 → 填密码 Aa → 提交 → 等房间窗口 → 校验房间名/密码
    fix()           一条龙：关提示 → （必要时先离房）→ 重建房间 → 校验
    ensure_ready()  给上层用的轻量入口：有弹窗就 fix，没弹窗什么都不做

CLI
---
    python map_update.py --scan       # 只查（命中 exit=2）
    python map_update.py --dismiss    # 只点确定
    python map_update.py --rebuild    # 只重建房间（假定提示已经关掉）
    python map_update.py --fix        # 一键处理（推荐）
    python map_update.py --probe      # 把所有 Qt 窗口存图到 logs/map_update_probe/

判据
----
模板 `templates/tpl_map_update_body.png`（正文「地图已更新，请重新下载地图」，24x214，
从 logs/popup_unknown_100150.png 裁的），灰度多尺度 0.70~1.60 共 19 档，阈值 **0.80**。

实测区分度（2026-11-18 10:26，扫 logs + kk_probe 共 100+ 张历史截图）：

| 画面 | 最高分 |
|---|---|
| 「地图已更新，请重新下载地图」480x300（4 张） | **1.000** |
| 平台主窗 / 房间窗 / 大厅 | 0.581 |
| 其它弹窗（断线重连、红包广告等） | 0.506 |

阈值 0.80 距误报上限 0.581 有 0.22 余量。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import cv2
import numpy as np

import busy_guard as bg          # 复用 Qt 窗口枚举 / 多尺度匹配 / 「确定」蓝块定位
import popup_guard as pg
import room_finder as rf

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
TPL_BODY = os.path.join(HERE, "templates", "tpl_map_update_body.png")
LOGS = os.path.join(HERE, "logs")

SCORE = 0.80                 # 正文命中阈值（误报上限 0.581）
ROOM_WAIT = 120              # 等房间窗口最多多少秒（地图大时下载会久一点）
ROOM_PWD = "Aa"
ROOM_NAME_KEY = "我在峡谷打"


def say(m=""):
    print(m, flush=True)


# --------------------------------------------------------------------- 识别
def _load_tpl():
    if not os.path.exists(TPL_BODY):
        return None
    return cv2.imdecode(np.fromfile(TPL_BODY, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)


def find(verbose=False):
    """找「地图已更新，请重新下载地图」弹窗。命中返回 dict，否则 None。"""
    tpl = _load_tpl()
    if tpl is None:
        if verbose:
            say(f"⚠️ 缺少模板 {TPL_BODY}")
        return None
    for w in bg._windows():                       # 按面积从小到大，先命中弹窗自己
        img = rf.grab_client(w["hwnd"], raise_win=False, settle=0.10)
        if img is None or not getattr(img, "size", 0):
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        sc, scale, loc, _ts = bg._match(gray, tpl)
        if verbose:
            say(f"  [地图更新] {w['w']}x{w['h']} hwnd={w['hwnd']} "
                f"标题={w['title'][:14]!r} score={sc:.3f}")
        if sc >= SCORE and loc is not None:
            return dict(hwnd=w["hwnd"], w=w["w"], h=w["h"], title=w["title"],
                        score=sc, scale=scale, tl=(int(loc[0]), int(loc[1])),
                        origin=w["origin"])
    return None


# --------------------------------------------------------------------- 关提示
def has_update_window(tol=40):
    """便宜预筛：有没有 480x300 左右的 Qt 小窗（只枚举，不做模板匹配，毫秒级）

    给「按 ESC 清场」用：屏幕上有地图更新提示时绝不能按 ESC —— ESC 会把提示关掉，
    平台就不触发下载了；要留给 fix() 去点「确定 + 重建房间」。
    """
    try:
        for w in bg._windows():
            if abs(w["w"] - 480) <= tol and abs(w["h"] - 300) <= tol:
                return True
    except Exception:
        pass
    return False


def dismiss(verbose=True, dry=False, tries=2):
    """点「确定」关掉「地图已更新」提示。返回 'none' / 'clicked' / 'dry' / 'stuck'"""
    hit = find(verbose=verbose)
    if not hit:
        return "none"
    if verbose:
        say(f"⛔ 检测到「地图已更新，请重新下载地图」窗口 {hit['w']}x{hit['h']} "
            f"score={hit['score']:.3f}")
    if dry:
        return "dry"
    for i in range(tries):
        bg.click_ok(hit, verbose=verbose)
        time.sleep(0.9)
        if not find():
            if verbose:
                say("   ✅ 提示已关闭（注意：这只关了提示，地图还没下载）")
            _log_event("map_update_dismissed", hit)
            return "clicked"
        if verbose:
            say(f"   还有弹窗（第 {i + 1} 次点击无效），再点一次…")
    return "stuck"


# --------------------------------------------------------------------- 建房重下
def _run(task, config="configs/wuxia.json", timeout=180, verbose=True):
    cmd = [PY, "-W", "ignore", "game_auto.py", "run",
           "--config", config, "--task", task, "--autostart"]
    if verbose:
        say(f"   ▸ {os.path.basename(config)} / {task}")
    p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    ok = p.returncode == 0
    if verbose:
        tail = [ln for ln in (p.stdout or "").splitlines() if ln.strip()][-4:]
        for ln in tail:
            say("     " + ln)
        if not ok:
            err = [ln for ln in (p.stderr or "").splitlines() if ln.strip()][-3:]
            for ln in err:
                say("     ! " + ln)
    return ok


def _leave_room(verbose=True):
    """当前若停在房间窗口，先点「退出」离房 —— 否则点「创建房间」会撞
    「您正在房间内，不能创建游戏房间」。"""
    room = rf.find_room(verbose=False, restore=False)
    if not room:
        return "no_room"
    if verbose:
        say(f"   当前在房间 hwnd={room['hwnd']}，先退出房间再重建")
    r = rf.leave_room(hwnd=room["hwnd"], verbose=verbose, tries=2)
    time.sleep(1.5)
    return r



def _ensure_platform_visible(verbose=True):
    """平台窗口最小化时 game_auto 会「找不到窗口」（client 0x0 被 min_size 过滤），
    所以建房前先把所有最小化的平台窗口还原，再把主窗（面积最大的那个）抓到前台。"""
    wins = rf.platform_windows(min_client=100, include_iconic=True)
    if not wins:
        if verbose:
            say("   ⚠️ 没找到平台窗口（平台可能没开）")
        return None
    for w in wins:
        if w.get("iconic"):
            try:
                if verbose:
                    say(f"   平台窗口被最小化 → 还原 hwnd={w['hwnd']}")
                rf.activate(w["hwnd"], settle=0.8)
            except Exception as e:
                if verbose:
                    say(f"   ⚠️ 还原 hwnd={w['hwnd']} 失败：{e!r}")
    wins = rf.platform_windows(min_client=100, include_iconic=True) or wins
    wins.sort(key=lambda w: (bool(w.get("iconic")),
                             -(w["client"][2] * w["client"][3])))
    main = wins[0]
    try:
        rf.activate(main["hwnd"], settle=0.5)
    except Exception:
        pass
    if verbose:
        say(f"   前台平台窗口 hwnd={main['hwnd']} {main['client'][2]}x{main['client'][3]}")
    return main["hwnd"]


def rebuild_room(verbose=True):
    """建房 → 填密码 → 提交 → 等房间窗口。返回 dict(ok, hwnd, ...)。

    这一步同时完成「地图重新下载」：房间窗口出现即代表平台已把新版地图拉下来。
    """
    t0 = time.time()
    _ensure_platform_visible(verbose=verbose)
    _leave_room(verbose=verbose)

    # ① 详情页点「创建房间」+ 填密码（create_room_pwd 自带「弹窗是否已开」的判断）
    if not _run("create_room_pwd", verbose=verbose):
        if verbose:
            say("   创建房间弹窗没打开 → 先回详情页（搜地图）再试一次")
        _run("search_double", verbose=verbose, timeout=150)
        if not _run("create_room_pwd", verbose=verbose):
            return dict(ok=False, why="create_form_failed")

    # ② 点「创建」→ 平台在建房的同时下载新版地图
    if not _run("submit_room", verbose=verbose):
        return dict(ok=False, why="submit_failed")

    # ③ 等房间窗口出现（= 地图下载完成 + 建房成功）
    hwnd = None
    deadline = time.time() + ROOM_WAIT
    last_note = 0.0
    while time.time() < deadline:
        room = rf.find_room(verbose=False, restore=False)
        if room:
            hwnd = room["hwnd"]
            break
        if verbose and time.time() - last_note >= 10:
            last_note = time.time()
            say(f"   ⏳ 等房间窗口（地图下载中）已等 {int(time.time() - t0)}s")
        time.sleep(2.0)

    if not hwnd:
        return dict(ok=False, why="room_timeout", waited=int(time.time() - t0))

    # ④ 校验房间名 / 密码
    ok_name = ok_pwd = None
    try:
        _h, info = rf.check_room(hwnd=hwnd, verbose=False)
        ok_name, ok_pwd = bool(info.get("name_ok")), bool(info.get("pwd_ok"))
    except Exception as e:
        if verbose:
            say(f"   ⚠️ 房间校验异常（不影响继续）：{e!r}")

    if verbose:
        say(f"   ✅ 房间已建好 hwnd={hwnd} 名/密码={ok_name}/{ok_pwd} "
            f"用时 {int(time.time() - t0)}s（地图已随建房更新）")
    _log_event("map_update_room_rebuilt",
               hwnd=hwnd, name_ok=ok_name, pwd_ok=ok_pwd,
               secs=int(time.time() - t0))
    return dict(ok=True, hwnd=hwnd, name_ok=ok_name, pwd_ok=ok_pwd,
                secs=int(time.time() - t0))


# --------------------------------------------------------------------- 一条龙
def fix(verbose=True, dry=False):
    """关提示 + 重建房间。返回 dict(status, ...)

    status:
      'no_popup_no_action'  没有这个弹窗（绝大多数情况，直接返回）
      'fixed'               关提示 + 房间已重建
      'popup_stuck'         弹窗点不掉
      'rebuild_failed'      建房失败（弹窗已关）
    """
    hit = find(verbose=verbose)
    if not hit:
        if verbose:
            say("✅ 没有「地图已更新」弹窗，无需处理")
        return dict(status="no_popup_no_action")
    if dry:
        return dict(status="dry", hwnd=hit["hwnd"], score=hit["score"])

    st = dismiss(verbose=verbose)
    if st == "stuck":
        return dict(status="popup_stuck", hwnd=hit["hwnd"])
    say("   ↳ 提示已关，开始重建房间（这一步才真正下载新版地图）")
    r = rebuild_room(verbose=verbose)
    if not r.get("ok"):
        return dict(status="rebuild_failed", why=r.get("why"), room=r)
    return dict(status="fixed", hwnd=r["hwnd"], name_ok=r.get("name_ok"),
                pwd_ok=r.get("pwd_ok"), secs=r.get("secs"))


def ensure_ready(verbose=True):
    """给 full_loop / pipeline 用的轻入口：有弹窗就处理，没有就直接过。"""
    hit = find(verbose=verbose)
    if not hit:
        return "clean"
    return fix(verbose=verbose).get("status", "unknown")


def _log_event(kind, hit=None, **kw):
    import json
    os.makedirs(LOGS, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
           "epoch": int(time.time()), "kind": kind}
    if hit:
        rec["window"] = {k: hit.get(k) for k in ("hwnd", "title", "w", "h", "score")}
    rec.update(kw)
    for p in (os.path.join(LOGS, "map_update_events.jsonl"),
              os.path.join(os.environ.get("TEMP", "."), "KKAuto", "map_update_events.jsonl")):
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            break
        except Exception:
            continue


# --------------------------------------------------------------------- CLI
def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="「地图已更新，请重新下载地图」守卫")
    ap.add_argument("--scan", action="store_true", help="只扫描（命中 exit=2）")
    ap.add_argument("--dismiss", action="store_true", help="只点确定关掉提示")
    ap.add_argument("--rebuild", action="store_true", help="只重建房间（触发地图下载）")
    ap.add_argument("--fix", action="store_true", help="一键：关提示 + 重建房间")
    ap.add_argument("--probe", action="store_true", help="把所有 Qt 窗口存图到 logs/map_update_probe/")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args(argv)

    pg.enable_dpi_awareness()

    if args.probe:
        out = os.path.join(LOGS, "map_update_probe")
        os.makedirs(out, exist_ok=True)
        for w in bg._windows():
            img = rf.grab_client(w["hwnd"], raise_win=True, settle=0.45)
            if img is None or not getattr(img, "size", 0):
                continue
            p = os.path.join(out, f"{w['hwnd']}_{w['w']}x{w['h']}.png")
            cv2.imencode(".png", img)[1].tofile(p)
            say(p)
        return 0

    if args.fix:
        r = fix(dry=args.dry)
        say(f"结果：{r}")
        return 0 if r["status"] in ("fixed", "no_popup_no_action") else 3

    if args.dismiss:
        r = dismiss(dry=args.dry)
        say(f"结果：{r}")
        return 0 if r in ("clicked", "none") else 3

    if args.rebuild:
        r = rebuild_room()
        say(f"结果：{r}")
        return 0 if r.get("ok") else 3

    hit = find(verbose=True)
    if not hit:
        say("✅ 没有「地图已更新，请重新下载地图」弹窗")
        return 0
    say(f"⛔ 命中：{hit['w']}x{hit['h']} score={hit['score']:.3f} hwnd={hit['hwnd']}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
