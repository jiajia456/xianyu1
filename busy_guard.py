"""僵尸会话守卫 —— 「平台提示 / 您正在游戏中，不能创建游戏房间。」

场景
----
强杀 War3（或游戏异常退出）之后，平台进程仍然认为「这一局还在进行中」，
此时点「创建房间」会弹这个提示，pipeline 就建不了房。
平台要过一段时间才会自己超时释放会话 —— 这个模块负责：

1. 认出这个弹窗（不只是「有个弹窗」，而是确实这一句）
2. 点「确定」把它关掉（不点 ✕，平台自绘窗点 ✕ 有时无响应）
3. **等 N 分钟**（默认 10）让平台把会话放掉，期间可被「停止」/「登录窗口」打断
4. 回来复扫：还在 → 继续等；没了 → 交给调用方重试建房

判据
----
模板 `templates/tpl_busy_body.png`（正文整行「您正在游戏中，不能创建游戏房间。」，
23x253，从 480x300 的弹窗截图上裁的），多尺度 0.70~1.60 共 19 档，阈值 **0.80**。

区分度实测（2026-11-17 12:05）：

| 窗口 | 最高分 |
|---|---|
| 「您正在游戏中，不能创建游戏房间」480x300 | **1.000** |
| 「断线重连失败」480x300 | 0.468 |
| 「断线重连」480x300 | 0.346 |
| 房间页 1224x904 | 0.522 |
| 平台主窗 1332x945 | 0.398 |
| 微信 880x640 | 0.403 |

阈值 0.80 距误报上限 0.522 有 0.28 余量。

被谁用
------
- `game_auto.cmd_pipeline`：建房失败（或建不出房间）时 → 确定 → 等 N 分钟 → 重试该步
- `engine.Loop.guard_busy()`：大循环里段失败后扫一次，命中就等待并重开
- CLI：`python busy_guard.py --scan|--dismiss|--wait 10`
"""
from __future__ import annotations

import os
import sys
import time

import cv2
import numpy as np

import popup_guard as pg
import room_finder as rf

try:
    import disc_guard as dg
except Exception:                                   # pragma: no cover
    dg = None

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_BUSY = os.path.join(HERE, "templates", "tpl_busy_body.png")
LOGS = os.path.join(HERE, "logs")

SCORE = 0.80                 # 正文命中阈值（误报上限 0.522）
MIN_W, MIN_H = 240, 160      # 弹窗客户区 480x300，比它小的不看
REF_OK = (240, 214)          # 参考弹窗（480x300）里「确定」按钮中心
DEFAULT_WAIT_MIN = 10        # 默认等 10 分钟
POLL_SEC = 20                # 等待期间每隔多久复扫一次弹窗是否还在


def say(m=""):
    print(m, flush=True)


# --------------------------------------------------------------------- 识别
def _load_tpl():
    if not os.path.exists(TPL_BUSY):
        return None
    return cv2.imdecode(np.fromfile(TPL_BUSY, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)


def _match(img, tpl, lo=0.70, hi=1.60, n=19):
    best = (0.0, 1.0, None, None)
    if img is None or not getattr(img, "size", 0):
        return best
    for sc in np.linspace(lo, hi, n):
        t = cv2.resize(tpl, None, fx=float(sc), fy=float(sc), interpolation=cv2.INTER_AREA)
        if t.shape[0] > img.shape[0] or t.shape[1] > img.shape[1]:
            continue
        r = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx > best[0]:
            best = (float(mx), float(sc), loc, t.shape)
    return best


def _windows():
    """候选窗口：可见、非最小化、尺寸在弹窗量级附近的 Qt 顶层窗"""
    import win32gui

    out = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
            return
        try:
            cls = win32gui.GetClassName(h) or ""
        except Exception:
            return
        if not cls.startswith("Qt"):
            return
        try:
            c = rf.client_rect(h)
        except Exception:
            return
        if c[2] < MIN_W or c[3] < MIN_H:
            return
        out.append(dict(hwnd=h, title=win32gui.GetWindowText(h) or "",
                        w=int(c[2]), h=int(c[3]), cls=cls,
                        origin=(int(c[0]), int(c[1]))))

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda x: x["w"] * x["h"])
    return out


def find(verbose=False):
    """找「您正在游戏中，不能创建游戏房间」弹窗。命中返回 dict，否则 None。

    按面积从小到大扫：弹窗自己 480x300，比压在它下面的大窗口小，
    先命中它才不会把点击坐标算到大窗口上。
    """
    tpl = _load_tpl()
    if tpl is None:
        if verbose:
            say(f"⚠️ 缺少模板 {TPL_BUSY}")
        return None
    for w in _windows():
        img = rf.grab_client(w["hwnd"], raise_win=False, settle=0.10)
        if img is None or not getattr(img, "size", 0):
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        sc, scale, loc, tsize = _match(gray, tpl)
        if verbose:
            say(f"  [正在游戏中] {w['w']}x{w['h']} hwnd={w['hwnd']} "
                f"标题={w['title'][:14]!r} score={sc:.3f}")
        if sc >= SCORE and loc is not None:
            return dict(hwnd=w["hwnd"], w=w["w"], h=w["h"], title=w["title"],
                        score=sc, scale=scale, tl=(int(loc[0]), int(loc[1])),
                        origin=w["origin"])
    return None


# --------------------------------------------------------------------- 动作
def _ok_center(hit):
    """「确定」按钮中心（客户区坐标）：先找蓝色实心块，找不到按参考比例算"""
    img = rf.grab_client(hit["hwnd"], raise_win=False, settle=0.05)
    if img is not None and getattr(img, "size", 0) and dg is not None:
        try:
            box = dg._blue_box(img)
        except Exception:
            box = None
        if box:
            return (int(box[0] + box[2] // 2), int(box[1] + box[3] // 2))
    sc = hit["w"] / 480.0
    return (int(REF_OK[0] * sc), int(REF_OK[1] * sc))


def click_ok(hit, verbose=True):
    """点「确定」把弹窗关掉（走强前台点击，避免 WorkBuddy 占前台时点空）"""
    cx, cy = _ok_center(hit)
    c = rf.client_rect(hit["hwnd"])
    if verbose:
        say(f"   → 点「确定」@客户区({cx},{cy}) → 屏幕({c[0] + cx},{c[1] + cy})")
    pg.click_screen(hit["hwnd"], c[0] + cx, c[1] + cy)
    return True


def dismiss(verbose=True, dry=False, tries=2):
    """关掉弹窗。返回 'none' / 'clicked' / 'dry' / 'stuck'"""
    hit = find(verbose=verbose)
    if not hit:
        return "none"
    if verbose:
        say(f"⛔ 检测到「您正在游戏中，不能创建游戏房间」 窗口 {hit['w']}x{hit['h']} "
            f"score={hit['score']:.3f}")
    if dry:
        return "dry"
    for i in range(tries):
        click_ok(hit, verbose=verbose)
        time.sleep(0.9)
        if not find():
            if verbose:
                say("   ✅ 弹窗已关闭")
            _log_event("busy_dismissed", hit)
            return "clicked"
        if verbose:
            say(f"   还有弹窗（第 {i + 1} 次点击无效），再点一次…")
    return "stuck"


def _log_event(kind, hit=None, **kw):
    import json
    os.makedirs(LOGS, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "epoch": int(time.time()), "kind": kind}
    if hit:
        rec["window"] = {k: hit.get(k) for k in ("hwnd", "title", "w", "h", "score")}
    rec.update(kw)
    try:
        with open(os.path.join(LOGS, "busy_events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------- 等待
def wait_clear(minutes=None, stop=None, login=None, interval=None, verbose=True):
    """点掉弹窗后等 N 分钟。

    返回：
      'clear'    —— 到点了，弹窗没有再出现（可以重试建房）
      'again'    —— 到点了，弹窗还在（调用方可再点一次「确定」并继续等）
      'stopped'  —— 用户点了停止
      'login'    —— 检测到登录窗口 / 顶号弹窗（立刻放弃等待）
    """
    minutes = DEFAULT_WAIT_MIN if minutes is None else float(minutes)
    interval = POLL_SEC if interval is None else max(5.0, float(interval))
    if minutes <= 0:
        return "clear"

    st = dismiss(verbose=verbose)
    if st == "stuck" and verbose:
        # 点两次都没关掉：不立刻放弃（可能只是这一下点击没生效，平台自己也可能在倒计时后关掉），
        # 照规则等满时间再复扫，仍然在就返回 'again' 交给调用方再点一轮。
        say("   ⚠️ 点了「确定」弹窗还在，仍按规则等待，之后再试")
    if st == "none" and verbose:
        say("   (已经没有这个弹窗了，仍然按规则等一会儿再建房)")

    deadline = time.time() + minutes * 60
    if verbose:
        say(f"   ⏳ 等 {minutes:g} 分钟让平台把游戏会话超时释放"
            f"（预计 {time.strftime('%H:%M:%S', time.localtime(deadline))} 结束）")
    _log_event("busy_wait_start", minutes=minutes)

    while time.time() < deadline:
        # 等待期间也能被「停止」和「登录窗」打断
        if stop is not None:
            try:
                if stop():
                    if verbose:
                        say("   ⏹ 等待被「停止」打断")
                    _log_event("busy_wait_stopped")
                    return "stopped"
            except Exception:
                pass
        if login is not None:
            try:
                rec = login()
                if rec:
                    if verbose:
                        say(f"   ⛔ 等待期间发现登录窗口（{rec.get('ts')}）→ 放弃等待")
                    _log_event("busy_wait_login", ts=rec.get("ts"))
                    return "login"
            except Exception:
                pass
        # 睡眠切片，保证 stop/login 反应不超过 5 秒
        remain_total = deadline - time.time()
        time.sleep(min(5.0, max(0.2, remain_total)))

    # 到点复扫
    hit = find(verbose=False)
    if hit:
        if verbose:
            say("   ⚠️ 等完了弹窗又出现 → 平台会话还没释放")
        _log_event("busy_still_there")
        return "again"
    if verbose:
        say("   ✅ 等待结束，弹窗没有再现（会话应该已释放）")
    _log_event("busy_wait_done")
    return "clear"


# --------------------------------------------------------------------- CLI
def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="「您正在游戏中，不能创建游戏房间」守卫")
    ap.add_argument("--scan", action="store_true", help="只扫描（命中 exit=2）")
    ap.add_argument("--dismiss", action="store_true", help="点确定关掉它")
    ap.add_argument("--wait", type=float, default=None, metavar="MIN",
                    help=f"关掉后等 N 分钟（默认 {DEFAULT_WAIT_MIN}）")
    ap.add_argument("--grab", action="store_true", help="把所有 Qt 窗口存图到 logs/busy_probe/")
    args = ap.parse_args(argv)

    pg.enable_dpi_awareness()

    if args.grab:
        out = os.path.join(LOGS, "busy_probe")
        os.makedirs(out, exist_ok=True)
        for w in _windows():
            img = rf.grab_client(w["hwnd"], raise_win=True, settle=0.45)
            if img is None or not getattr(img, "size", 0):
                continue
            p = os.path.join(out, f"{w['hwnd']}_{w['w']}x{w['h']}.png")
            cv2.imencode(".png", img)[1].tofile(p)
            say(p)
        return 0

    if args.wait is not None:
        r = wait_clear(args.wait)
        say(f"结果：{r}")
        return 0 if r in ("clear",) else 3

    if args.dismiss:
        r = dismiss(verbose=True)
        say(f"结果：{r}")
        return 0 if r in ("clicked", "none") else 3

    hit = find(verbose=True)
    if not hit:
        say("✅ 没有「您正在游戏中，不能创建游戏房间」弹窗")
        return 0
    say(f"⛔ 命中：{hit['w']}x{hit['h']} score={hit['score']:.3f} hwnd={hit['hwnd']}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
