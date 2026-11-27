# -*- coding: utf-8 -*-
"""断线重连守卫（War3 被强杀 / 崩溃 / 掉线后的收尾）

用户 2026-11-17 11:19 + 11:35 教的两步规则：
    1. 平台弹「断线重连 / 您先前的游戏还在进行中，是否需要断线重连？ / 断线重连(25) | 取消」
       → **第一时间点「取消」**（绝不能点「断线重连」，那会去连一局已经死掉的游戏）
    2. 之后若弹「平台提示 / 断线重连失败。」→ **点「确定」**
    3. 然后按房间流程「退出房间」

判据
----
① 断线重连弹窗：正文行模板 `templates/tpl_disc_body.png`
   （「您先前的游戏还在进行中，是否需要断线重连？」，480x300 截图里裁的 22x378，
   原点在参考图 (56,110)），0.70~1.60 共 19 档多尺度匹配，score ≥ 0.72 命中。
   ⚠️ 不能拿标题「断线重连」四个字当判据：微信灰气泡实测能到 0.980，会点错。
② 断线重连失败弹窗：模板 `templates/tpl_discfail_body.png`（「断线重连失败。」，32x150），
   同样是同一套多尺度匹配，score ≥ 0.72。

两个必须踩过的坑
----------------
- **遮挡**：480x300 弹窗经常压在微信上面，抓微信的客户区会抓到弹窗像素（微信也能拿 0.96 分）。
  所以命中后要**取面积最小的那个窗口**——它才是弹窗自己；否则点击坐标会算到别的窗口上。
- **前台锁**：`SetForegroundWindow` 在 WorkBuddy 处于前台时会被系统拒绝，点击落到 WorkBuddy 上
  （实测点「确定」4 次毫无反应）。必须用 `popup_guard.force_foreground()`（AttachThreadInput）
  再点，才会真的落到弹窗上。

被谁用
------
- ~~`disc_guard.recover()`~~：2026-11-27 17:00 用户要求删除（不再由断线弹窗触发重启平台）
- 引擎：局内异常 / War3 进程没了之后调用一次
- CLI：`python disc_guard.py --scan|--dismiss|--grab`
"""
from __future__ import annotations

import os
import sys
import time

import cv2
import numpy as np

import popup_guard as pg
import room_finder as rf

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_BODY = os.path.join(HERE, "templates", "tpl_disc_body.png")
TPL_FAIL = os.path.join(HERE, "templates", "tpl_discfail_body.png")
TPL_CANCEL = os.path.join(HERE, "templates", "tpl_disc_cancel.png")

SCORE = 0.72                 # 正文命中阈值（微信/房间页/主窗误报上限 0.42）
CANCEL_SCORE = 0.65          # 取消按钮模板校验阈值
REF_BODY_TL = (56, 110)      # 裁正文时在参考弹窗里的左上角
REF_CANCEL_C = (332, 214)    # 参考弹窗里「取消」按钮中心
MIN_W, MIN_H = 240, 160      # 太小的窗口不看
LOGS = os.path.join(HERE, "logs")


def say(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _load(path):
    if not os.path.exists(path):
        return None
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


_TPL = {}


def _tpl(path):
    if path not in _TPL:
        _TPL[path] = _load(path)
    return _TPL[path]


def match_multi(img, tpl, lo=0.70, hi=1.60, n=19):
    """多尺度模板匹配 → (score, scale, (x, y), (w, h))。窗口/DPI 不同尺寸会变。"""
    if img is None or not getattr(img, "size", 0) or tpl is None:
        return 0.0, 1.0, None, None
    best = (0.0, 1.0, None, None)
    for sc in np.linspace(lo, hi, n):
        t = cv2.resize(tpl, None, fx=float(sc), fy=float(sc), interpolation=cv2.INTER_AREA)
        if t.shape[0] > img.shape[0] or t.shape[1] > img.shape[1] or t.shape[0] < 6:
            continue
        res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx > best[0]:
            best = (float(mx), float(sc), loc, (t.shape[1], t.shape[0]))
    return best


def _qt_windows(popup_only=False):
    """所有可见的 Qt 顶层窗口（含 480x300 小弹窗）。

    不用 rf.platform_windows：它按 500px 过滤，弹窗会被直接排除。
    `popup_only=True` 时只留「弹窗体型」的窗口（宽 300~1000、高 200~700、宽高比 1.45~1.75），
    实测弹窗 480x300 = 1.60，而房间窗 1224x904 = 1.35、微信 880x640 = 1.375 都会被排掉，
    一次检查从 5 秒降到不到 1 秒（每段开跑前都要扫，不能太慢）。
    """
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
        w, hh = int(c[2]), int(c[3])
        if w < MIN_W or hh < MIN_H:
            return
        if popup_only:
            if not (300 <= w <= 1000 and 200 <= hh <= 700):
                return
            if not (1.45 <= w / float(hh) <= 1.75):
                return
        out.append(dict(hwnd=h, title=win32gui.GetWindowText(h) or "",
                        w=w, h=hh, cls=cls, origin=(int(c[0]), int(c[1]))))

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda x: x["w"] * x["h"], reverse=True)
    return out


def _find_by(tpl_path, verbose=False, tag="", popup_only=True, verify=None):
    """在所有可见 Qt 窗口里找模板。命中返回 dict（取面积最小的命中窗口），否则 None。

    `verify(img)` 返回 False 的候选会被丢掉（用于按版式二次确认）。
    """
    tpl = _tpl(tpl_path)
    if tpl is None:
        if verbose:
            say(f"⚠️ 缺少模板 {tpl_path}")
        return None
    hits = []
    for w in _qt_windows(popup_only=popup_only):
        img = rf.grab_client(w["hwnd"], raise_win=False, settle=0.10)
        if img is None or not getattr(img, "size", 0):
            continue
        sc, scale, loc, tsize = match_multi(img, tpl)
        if verbose:
            say(f"  {tag}{w['w']}x{w['h']} hwnd={w['hwnd']} 标题={w['title'][:12]!r} score={sc:.3f}")
        if sc < SCORE or loc is None:
            continue
        if verify is not None and not verify(img):
            if verbose:
                say(f"     ↳ 版式不符（蓝+灰双按钮），不是{tag.strip() or '目标'}弹窗，跳过")
            continue
        hits.append(dict(hwnd=w["hwnd"], w=w["w"], h=w["h"], title=w["title"],
                         score=sc, scale=scale, tl=(int(loc[0]), int(loc[1])),
                         origin=w["origin"], area=w["w"] * w["h"]))
    if not hits:
        return None
    # 面积最小的才是弹窗自己；靠上的大窗口只是「被弹窗盖住」，抓图里混进了弹窗像素
    hits.sort(key=lambda h: h["area"])
    hit = hits[0]
    hit["cancel"] = _cancel_center(rf.grab_client(hit["hwnd"], raise_win=False, settle=0.0),
                                   hit["tl"], hit["scale"])
    return hit


def _verify_fail_layout(img):
    """「断线重连失败」= 只有一个居中的蓝色「确定」，右边没有深灰「取消」"""
    box = _blue_box(img)
    if box is None:
        return False
    if _has_gray_right(img, box):
        return False
    H, W = img.shape[:2]
    cx = box[0] + box[2] / 2.0
    return abs(cx - W / 2.0) <= 0.08 * W


def find(verbose=False, popup_only=True):
    """找「断线重连」弹窗"""
    return _find_by(TPL_BODY, verbose=verbose, tag="[断线重连] ", popup_only=popup_only)


def find_fail(verbose=False, popup_only=True):
    """找「断线重连失败」弹窗（文字 + 单按钮版式双重确认）"""
    return _find_by(TPL_FAIL, verbose=verbose, tag="[重连失败] ",
                    popup_only=popup_only, verify=_verify_fail_layout)


def _cancel_center(img, tl, scale):
    """先按偏移算，再用取消按钮模板在附近校正"""
    ox = int(tl[0] + (REF_CANCEL_C[0] - REF_BODY_TL[0]) * scale)
    oy = int(tl[1] + (REF_CANCEL_C[1] - REF_BODY_TL[1]) * scale)
    tpl = _tpl(TPL_CANCEL)
    if tpl is not None and img is not None and img.size:
        H, W = img.shape[:2]
        pad_x, pad_y = int(260 * scale), int(200 * scale)
        x0, x1 = max(0, ox - pad_x), min(W, ox + pad_x)
        y0, y1 = max(0, oy - pad_y), min(H, oy + pad_y)
        roi = img[y0:y1, x0:x1]
        if roi.size:
            sc, scale2, loc, tsize = match_multi(roi, tpl, lo=0.60, hi=1.70, n=23)
            if sc >= CANCEL_SCORE and loc and tsize:
                return (int(x0 + loc[0] + tsize[0] // 2),
                        int(y0 + loc[1] + tsize[1] // 2), f"模板{sc:.2f}")
    return (ox, oy, "偏移")


def _blue_center(img, min_area=2000, min_w=80):
    """找弹窗里那个蓝色实心按钮（确定）中心"""
    box = _blue_box(img, min_area=min_area, min_w=min_w)
    if box is None:
        return None
    return (int(box[0] + box[2] // 2), int(box[1] + box[3] // 2))


def _blue_box(img, min_area=2000, min_w=80):
    """找弹窗里最大的蓝色实心按钮 → (x, y, w, h)"""
    if img is None or not getattr(img, "size", 0):
        return None
    b, g, r = img[:, :, 0].astype(int), img[:, :, 1].astype(int), img[:, :, 2].astype(int)
    mask = ((b > 170) & (r < 115) & (g > 85) & (g < 195)).astype(np.uint8)
    if mask.sum() < min_area:
        return None
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    best = None
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if a < min_area or w < min_w:
            continue
        if best is None or a > best[4]:
            best = (int(x), int(y), int(w), int(h), int(a))
    return None if best is None else best[:4]


def _has_gray_right(img, box):
    """蓝色按钮右边是否紧跟一个同高的深灰按钮 —— 有就是「断线重连(25) | 取消」两个按钮的版式，
    没有才是「断线重连失败」那种只有一个居中「确定」的版式。

    ⚠️ 为什么不能只靠文字模板：「断线重连失败。」和「…是否需要断线重连？」共用「断线重连」四个字，
    实测互相能拿到 **0.788**，会把手上有倒计时的重连弹窗误判成失败提示。
    """
    if img is None or box is None:
        return False
    H, W = img.shape[:2]
    x, y, w, h = box
    px0, px1 = x + w + 3, min(W, x + int(w * 1.75))
    if px1 - px0 < 20:
        return False
    reg = img[max(0, y):min(H, y + h), px0:px1].astype(int)
    if reg.size == 0:
        return False
    rb, rg, rr = reg[:, :, 0], reg[:, :, 1], reg[:, :, 2]
    mean = (rb + rg + rr) / 3.0
    grayish = (np.abs(rb - rg) < 18) & (np.abs(rg - rr) < 18) & (mean > 33) & (mean < 112)
    return bool(grayish.mean() >= 0.35)


def _click_client(hit, cx, cy, what=""):
    """客户区坐标 → 屏幕坐标，用强前台点击（SetForegroundWindow 被前台锁拒绝会点空）"""
    ox, oy = hit["origin"]
    pg.click_screen(hit["hwnd"], ox + cx, oy + cy)
    if what:
        say(f"   点{what} @client({cx},{cy}) → screen({ox + cx},{oy + cy})")


def _press_esc(hit, checker=None, verbose=False):
    """先按 ESC 试关弹窗（用户 2026-11-26 定的优先级：ESC 优先，关不掉才点按钮）。

    为什么必须先 ESC：这个弹窗的倒计时在「断线重连(16)」那个蓝按钮上，**有概率会
    真的去重连** —— 连进去就是一局已经结束的游戏，下一局重开也跟着坏。ESC 实测能
    直接关掉，而且不碰任何按钮，不会误触发。
    返回 True = 弹窗已经没了（ESC 成功）。
    """
    # 切不到前台就别发 ESC：pydirectinput 发的是全局键盘事件，焦点没在弹窗上时
    # 这个 ESC 会打到当前前台窗口（典型的 War3）→ 游戏里弹出菜单，反而更糟。
    try:
        if not pg.force_foreground(hit["hwnd"]):
            if verbose:
                say("   (弹窗窗口切不到前台 → 跳过 ESC，直接点按钮)")
            return False
    except Exception:
        return False
    try:
        import pydirectinput as pdi
    except Exception:
        return False
    try:
        pdi.press("esc")
    except Exception:
        try:
            pdi.keyDown("esc")
            time.sleep(0.05)
            pdi.keyUp("esc")
        except Exception:
            return False
    time.sleep(0.7)
    try:
        return (checker or find)(verbose=False) is None
    except Exception:
        return False


def dismiss(verbose=True, dry=False, tries=3):
    """关掉「断线重连」弹窗。返回 'esc' / 'clicked' / 'none' / 'dry' / 'fail'

    2026-11-26 用户定：**优先按 ESC**（倒计时在「断线重连」按钮上，有概率自动重连），
    ESC 之后弹窗还在才点「取消」。
    """
    hit = find(verbose=verbose)
    if not hit:
        if verbose:
            say("没有「断线重连」弹窗")
        return "none"
    if dry:
        return "dry"
    if _press_esc(hit, verbose=verbose):
        if verbose:
            say("   ⌨ ESC 已关掉「断线重连」弹窗（未点任何按钮，不会触发重连）")
        return "esc"
    for i in range(tries):
        hit = find(verbose=verbose and i == 0)
        if not hit:
            if i == 0:
                if verbose:
                    say("没有「断线重连」弹窗")
                return "none"
            return "clicked"
        cx, cy, how = hit["cancel"]
        if verbose:
            say(f"⛔ 断线重连弹窗（ESC 没关掉）score={hit['score']:.3f} "
                f"scale={hit['scale']:.2f} 窗口 {hit['w']}x{hit['h']} "
                f"→ 取消@{cx},{cy}（{how}）")
        if dry:
            return "dry"
        _click_client(hit, cx, cy, "「取消」")
        time.sleep(1.0)
    return "clicked" if not find() else "fail"


def dismiss_fail(verbose=True, dry=False, tries=3):
    """关掉「断线重连失败」提示。返回 'esc' / 'clicked' / 'none' / 'dry' / 'fail'

    2026-11-26 用户定：**优先按 ESC**，ESC 之后还在才点「确定」。
    """
    hit = find_fail(verbose=verbose)
    if not hit:
        if verbose:
            say("没有「断线重连失败」提示")
        return "none"
    if dry:
        return "dry"
    if _press_esc(hit, checker=find_fail, verbose=verbose):
        if verbose:
            say("   ⌨ ESC 已关掉「断线重连失败」提示（未点任何按钮）")
        return "esc"
    for i in range(tries):
        hit = find_fail(verbose=verbose and i == 0)
        if not hit:
            if i == 0:
                if verbose:
                    say("没有「断线重连失败」提示")
                return "none"
            return "clicked"
        img = rf.grab_client(hit["hwnd"], raise_win=False, settle=0.0)
        ok = _blue_center(img)
        if ok is None:
            if verbose:
                say("⚠️ 断线重连失败弹窗里没找到蓝色「确定」按钮")
            return "fail"
        if verbose:
            say(f"⛔ 断线重连失败 score={hit['score']:.3f} 窗口 {hit['w']}x{hit['h']}")
        if dry:
            return "dry"
        _click_client(hit, ok[0], ok[1], "「确定」")
        time.sleep(1.0)
    return "clicked" if not find_fail() else "fail"


def rejoin(verbose=True, timeout=90):
    """仅在确认双按钮的断线重连弹窗后，尝试接回同一局。

    返回 rejoined / no_popup / invalid_layout / running / failed / timeout。
    这里绝不按 ESC、点取消、退出房间或强杀 War3。
    """
    hit = find(verbose=verbose)
    if hit is None:
        return "no_popup"
    # 弹窗正文匹配之外再核对蓝色重连按钮和右侧灰色取消按钮。
    img = rf.grab_client(hit["hwnd"], raise_win=False, settle=0.0)
    blue = _blue_box(img)
    if blue is None or not _has_gray_right(img, blue):
        say("⛔ 重连弹窗按钮版式无法确认，停止点击")
        return "invalid_layout"
    import game_auto
    if game_auto.war3_alive():
        say("   War3 进程仍在，先保留当前游戏，不重复点击重连")
        return "running"
    cx, cy = blue[0] + blue[2] // 2, blue[1] + blue[3] // 2
    say(f"   尝试断线重连 @client({cx},{cy})")
    _click_client(hit, cx, cy, "「断线重连」")
    deadline = time.monotonic() + max(1, timeout)
    while time.monotonic() < deadline:
        time.sleep(2)
        if game_auto.war3_alive():
            _log_event("rejoin", result="rejoined", hwnd=hit["hwnd"])
            say("   ✓ War3 进程已恢复，继续等待进入游戏")
            return "rejoined"
        if find_fail(verbose=False):
            _log_event("rejoin", result="failed", hwnd=hit["hwnd"])
            say("   ⛔ 平台提示断线重连失败")
            return "failed"
    _log_event("rejoin", result="timeout", hwnd=hit["hwnd"])
    say("   ⛔ 重连后等待 War3 超时，保留房间现场")
    return "timeout"


def _log_event(kind, **kw):
    """把「断线重连」处理写进 logs/disconnect_events.jsonl（时间和现象都要留痕）"""
    import json
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "epoch": int(time.time()), "kind": kind}
    rec.update(kw)
    try:
        os.makedirs(LOGS, exist_ok=True)
        with open(os.path.join(LOGS, "disconnect_events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def grab_all(outdir=None):
    """把当前所有可见 Qt 窗口存图（弹窗真出现时用来裁模板/核对）"""
    outdir = outdir or os.path.join(LOGS, "disc_probe")
    os.makedirs(outdir, exist_ok=True)
    saved = []
    for w in _qt_windows():
        img = rf.grab_client(w["hwnd"], raise_win=True, settle=0.45)
        if img is None or not getattr(img, "size", 0):
            continue
        p = os.path.join(outdir, f"{w['hwnd']}_{w['w']}x{w['h']}.png")
        cv2.imencode(".png", img)[1].tofile(p)
        saved.append(p)
    return saved


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    pg.enable_dpi_awareness()
    if "--scan" in argv:
        a = find(verbose=True)
        b = find_fail(verbose=True)
        say(f"断线重连={'命中' if a else '无'}  重连失败={'命中' if b else '无'}")
        return 2 if (a or b) else 0
    if "--dismiss" in argv:
        r = dismiss()
        print(f"dismiss={r}")
        return 0 if r in ("clicked", "none") else 1
    if "--grab" in argv:
        print("\n".join(grab_all()))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
