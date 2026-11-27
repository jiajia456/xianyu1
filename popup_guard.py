# -*- coding: utf-8 -*-
"""
随机弹窗守卫 —— 平台/游戏上突然冒出来的活动弹窗（首充礼包、限时活动、公告…），
看到右上角 X 就点掉，别让它挡住下一轮开局的点击。

用户 2026-11-16 22:42 需求：
  「退出又出现随机弹窗事件，看到直接点 X 关闭就可以了，随机弹窗事件，
    记录为随机弹窗方便归类」

做法：
  1) 枚举窗口（默认标题含「KK官方对战平台」的窗口，加 --war3 连游戏窗一起看）
  2) 每个窗口里按 configs/popup.json 里登记的模板找弹窗
  3) 命中就找 X（templates/btn_popup_x.png），找不到 X 就按弹窗右上角的相对坐标兜底
  4) 点掉 → 打印一行 → 追加一条记录到 logs/popup_events.jsonl（kind=随机弹窗）
  5) 没登记的弹窗也不放过：窗口尺寸明显不是已知几种时，落一张 logs/popup_unknown_*.png
     并记一条 kind=未知弹窗，方便以后补模板归类

这里的弹窗都是**独立窗口**（PrintWindow 能抓到内容），点击前用 ClientToScreen 把
窗口客户区坐标换成屏幕坐标。

2026-11-17 21:2x 新增两件事（用户遇到的「216U币 红包」独立小窗）：
  * 模板支持 scales 多尺度 —— 换电脑 / 改 DPI 后窗口尺寸变了也照样命中
  * 模板支持 min_mean / min_light 亮度硬校验 —— 只靠相似度会误报：
    logs/plat_1080.png 上那块深色小面板能到 0.822（真弹窗是白卡：均值 184 / 亮像素 67%，
    误命中区域只有 26 / 0%），不校验就会去点不该点的地方。

用法：
  python popup_guard.py --close              # 关掉当前所有已知弹窗（最多 3 个）
  python popup_guard.py --close --quiet      # 没有弹窗时不输出（给循环里静默调用）
  python popup_guard.py --scan               # 只扫不点，报告看到了什么
  python popup_guard.py --scan --war3        # 连 War3 窗口一起扫
  python popup_guard.py --check logs/x.png   # 用已有截图调试模板（不绑窗口）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import cv2
import numpy as np
import win32api
import win32con
import win32gui
import win32process
import win32ui
import ctypes
from ctypes import windll

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(HERE, "logs", "popup_events.jsonl")
DEF_CFG = os.path.join(HERE, "configs", "popup.json")
UNKNOWN_DIR = os.path.join(HERE, "logs")

KIND_RANDOM = "随机弹窗"
KIND_UNKNOWN = "未知弹窗"

# 已知的「不是弹窗」的窗口尺寸（主平台 1332x945 / 房间 1224x904 / 小窗 924x562 是首充弹窗本身）
KNOWN_SIZES = {(1332, 945), (1224, 904), (1690, 921), (1440, 900), (1078, 760),
                (584, 517)}   # 584x517 = 平台「创建房间」对话框（2026-11-18 18:15 被误记成未知弹窗）

# 「主窗」判据：面积 >= 80 万像素。实测主窗 1224x904~1332x945（110 万+），
# 而弹窗都是独立小窗：红包 415x448(19万)、我要变强 754x626(47万)、平台提示 480x300(14万)。
# 用面积而不是宽高阈值 —— 754x626 比 700 宽，用宽度判会被误当成主窗跳过。
POPUP_MAX_AREA = 800000

_UNK_SEEN = {}          # (hwnd, size) -> 上次存证时间，防止同一窗口反复存图记账
UNK_GAP = 120.0


def enable_dpi_awareness():
    try:
        windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ---------------------------------------------------------------- 截图
def grab_hwnd(hwnd) -> np.ndarray | None:
    """优先 PrintWindow（窗口被遮挡也行），失败退回屏幕抓取。
    句柄可能在拿到后立刻被销毁（弹窗自动关闭），所以先验活 + 全局兜异常。"""
    try:
        if not win32gui.IsWindow(hwnd):
            return None
        l, t, r, b = win32gui.GetClientRect(hwnd)
    except Exception:
        return None
    w, h = r, b
    if w <= 0 or h <= 0:
        return None
    try:
        hdc = win32gui.GetWindowDC(hwnd)
        mfc = win32ui.CreateDCFromHandle(hdc)
        mem = mfc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc, w, h)
        mem.SelectObject(bmp)
        res = windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 3)
        arr = bmp.GetBitmapBits(True)
        img = np.frombuffer(arr, dtype=np.uint8).reshape(h, w, 4)
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        mfc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)
        if res and img.size and img.std() > 3:
            return img
    except Exception:
        pass
    # 退路：直接抓屏幕
    try:
        from PIL import ImageGrab
        x, y = win32gui.ClientToScreen(hwnd, (0, 0))
        arr = np.array(ImageGrab.grab(bbox=(x, y, x + w, y + h)))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR) if arr.size else None
    except Exception:
        return None


def _rd(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def match(img, tpl_path, score=0.82):
    """单尺度模板匹配，返回 (score, (cx, cy), (x, y, w, h))；模板或图为空则 None"""
    if img is None or img.size == 0:
        return None
    tpl = _rd(tpl_path)
    if tpl is None or tpl.shape[0] > img.shape[0] or tpl.shape[1] > img.shape[1]:
        return None
    res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    hh, ww = tpl.shape[:2]
    return mx, (loc[0] + ww // 2, loc[1] + hh // 2), (loc[0], loc[1], ww, hh)


def match_any(img, p):
    """按登记表匹配：配了 scales 就走多尺度（换电脑/改 DPI 也能命中）

    scales 写成 [下限, 上限, 步数]，例如 [0.70, 1.60, 10]。返回格式同 match()。
    """
    if img is None or not getattr(img, "size", 0):
        return None
    tpl_path = os.path.join(HERE, p["tpl"])
    sc = p.get("scales")
    if not sc:
        return match(img, tpl_path, p.get("score", 0.82))
    tpl0 = _rd(tpl_path)
    if tpl0 is None:
        return None
    if isinstance(sc, (list, tuple)):
        vals = list(sc) + [0.70, 1.60, 10]
        lo, hi, n = vals[0], vals[1], vals[2]
    else:
        lo, hi, n = 0.70, 1.60, 10
    best = None
    for s in np.linspace(float(lo), float(hi), max(2, int(n))):
        t = cv2.resize(tpl0, None, fx=float(s), fy=float(s), interpolation=cv2.INTER_AREA)
        if t.shape[0] > img.shape[0] or t.shape[1] > img.shape[1] or min(t.shape[:2]) < 6:
            continue
        res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if best is None or mx > best[0]:
            hh, ww = t.shape[:2]
            best = (float(mx), (loc[0] + ww // 2, loc[1] + hh // 2),
                    (loc[0], loc[1], ww, hh))
    return best


def _patch_ok(img, box, p):
    """命中区域的「长相」硬校验（配了 min_mean / min_light 才生效）

    只靠相似度会误报 —— logs/plat_1080.png 里那块深色小面板带白 X，红包模板能到 0.822；
    真弹窗是白卡（均值 184 / 亮像素 67%），误命中区域只有 26 / 0%，一刀就能分开。
    """
    mm, ml = p.get("min_mean"), p.get("min_light")
    if mm is None and ml is None:
        return True
    if img is None or not getattr(img, "size", 0) or not box:
        return False
    x, y, w, h = [int(v) for v in box[:4]]
    patch = img[max(0, y):y + h, max(0, x):x + w]
    if not getattr(patch, "size", 0):
        return False
    g = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    if mm is not None and g.mean() < float(mm):
        return False
    if ml is not None and (g > 180).mean() < float(ml):
        return False
    return True


def _hit_ok(img, m, p):
    """命中是否可用：相似度过阈值 + 区域长相校验过"""
    if not m:
        return False
    if m[0] < p.get("score", 0.82):
        return False
    return _patch_ok(img, m[2], p)


# ---------------------------------------------------------------- 点击
def _responsive(hwnd, timeout_ms=300) -> bool:
    """窗口还会处理消息吗（WM_NULL + SMTO_ABORTIFHUNG，超时就当挂起）

    挂起的窗口**绝对不能 AttachThreadInput**：目标线程不处理消息，
    AttachThreadInput / BringWindowToTop 会一起卡死（2026-11-17 实测卡过 6 分钟）。
    """
    try:
        res = ctypes.c_size_t(0)
        r = windll.user32.SendMessageTimeoutW(
            hwnd, 0, 0, 0, 0x0002, int(timeout_ms), ctypes.byref(res))
        return bool(r)
    except Exception:
        return True


def force_foreground(hwnd, tries=3):
    """强置前台。SetForegroundWindow 单独调经常被系统拒（前台锁定），
    要先 AttachThreadInput 到当前前台线程才行 —— 否则鼠标点击会落到盖在上面的窗口（WorkBuddy）。"""
    u = windll.user32
    k = windll.kernel32
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)
    except Exception:
        pass
    if not _responsive(hwnd):
        # 窗口挂起：只试一次普通置前，不做 AttachThreadInput（会互相卡死）
        try:
            u.SetForegroundWindow(hwnd)
        except Exception:
            pass
        return u.GetForegroundWindow() == hwnd
    for _ in range(max(1, tries)):
        if u.GetForegroundWindow() == hwnd:
            return True
        fg = u.GetForegroundWindow()
        cur = k.GetCurrentThreadId()
        fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
        tgt_tid = u.GetWindowThreadProcessId(hwnd, None)
        if fg_tid:
            u.AttachThreadInput(cur, fg_tid, True)
        if tgt_tid:
            u.AttachThreadInput(cur, tgt_tid, True)
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        except Exception:
            pass
        if fg_tid:
            u.AttachThreadInput(cur, fg_tid, False)
        if tgt_tid:
            u.AttachThreadInput(cur, tgt_tid, False)
        time.sleep(0.18)
    return u.GetForegroundWindow() == hwnd


def click_screen(hwnd, sx, sy, hold=0.06):
    """把窗口提到前台后在**屏幕坐标**点一下（带一点随机，别点得跟机器一样死板）"""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)
        if not force_foreground(hwnd):
            win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.25)
    jx = int(np.random.randint(-2, 3))
    jy = int(np.random.randint(-2, 3))
    x, y = int(sx + jx), int(sy + jy)
    win32api.SetCursorPos((x, y))
    time.sleep(0.12)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(hold)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


# ---------------------------------------------------------------- 记录
def log_event(kind, name, hwnd, size, action, extra=None):
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "name": name,
           "hwnd": int(hwnd), "size": list(size or []), "action": action}
    if extra:
        rec.update(extra)
    try:
        os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


# ---------------------------------------------------------------- 窗口枚举
def enum_candidates(include_war3=False):
    """返回 [(hwnd, title, (w, h))]：标题含「KK官方对战平台」（可选 War3）的可见窗口"""
    out = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        title = win32gui.GetWindowText(h) or ""
        low = title.lower()
        # 我们自己的界面标题是「我在峡谷打僵尸 · 三大流程循环」（旧名 KK 自动打图），也以 kk 开头，
        # 不排除的话每轮都会被记成「未知弹窗」并存一张截图（2026-11-17 17:03 实测踩到）
        if "自动打图" in title or "峡谷打僵尸" in title:
            return
        if "对战平台" in title:
            pass
        elif low.startswith("kk"):
            # 2026-11-20 01:20：标题以 kk 开头的**外部窗口**（资源管理器「KK日志_xxx」文件夹）
            # 曾被当成平台候选 → 签到把它当签到弹窗直接放弃。平台 UI 全是 Qt 窗口，
            # 所以 kk 前缀还必须同时是 Qt 类名才认。
            try:
                if not (win32gui.GetClassName(h) or "").startswith("Qt"):
                    return
            except Exception:
                return
        elif include_war3 and low.startswith("warcraft"):
            pass
        else:
            return
        r = win32gui.GetClientRect(h)
        if r[2] < 300 or r[3] < 300:
            return
        out.append((h, title, (r[2], r[3])))

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda x: -x[2][0] * x[2][1])
    return out


# ---------------------------------------------------------------- 主逻辑
def load_popups(cfg_path):
    try:
        with open(cfg_path, encoding="utf-8") as f:
            j = json.load(f)
        return [p for p in (j.get("popups") or []) if p.get("enabled", True)]
    except Exception:
        # 兜底：至少认得首充礼包
        return [{"name": "首充礼包", "kind": KIND_RANDOM,
                 "tpl": "templates/tpl_promo_firstcharge.png",
                 "score": 0.82, "close_tpl": "templates/btn_popup_x.png",
                 "close_rel": [0.961, 0.048]}]


def _is_blank(img, std_min=6.0, uniq_max=8):
    """未渲染帧判定：画面几乎纯色（std 极低 + 灰度层级极少）。

    2026-11-17 23:12 实测：弹窗刚创建时抓到的图 std=2.92、灰度唯一值 4 个；
    正常弹窗 std=13~49。这类存证没有价值，会污染案例库。
    """
    if img is None or not getattr(img, "size", 0):
        return True
    try:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    except Exception:
        return False
    if g.std() >= std_min:
        return False
    return int(len(np.unique(g))) <= uniq_max


def scan_once(popups, include_war3=False, verbose=True):
    """扫一遍，返回 [(hwnd, title, size, popup, img, match)]；popup=None 表示「不认识但像弹窗」"""
    cands = enum_candidates(include_war3)
    main_area = max((s[0] * s[1] for _, _, s in cands), default=0)
    hits = []
    for hwnd, title, size in cands:
        img = grab_hwnd(hwnd)
        if img is None:
            continue
        for p in popups:
            # only_small：该弹窗只会以独立小窗出现，主窗内的画面不参与匹配
            # （2026-11-17 22:21 实测：主窗 1332x945 里一块亮图相似度 0.905 被误判，点了空处）
            if p.get("only_small") and size[0] * size[1] >= POPUP_MAX_AREA:
                continue
            m = match_any(img, p)
            if _hit_ok(img, m, p):
                hits.append((hwnd, title, size, p, img, m))
                if verbose:
                    print(f"   🔎 命中「{p['name']}」 score={m[0]:.3f} "
                          f"hwnd={hwnd} {size[0]}x{size[1]}", flush=True)
                break
            if verbose and m and m[0] >= p.get("score", 0.82):
                print(f"   （「{p['name']}」相似度 {m[0]:.3f} 但区域不像弹窗，跳过）",
                      flush=True)
        else:
            # 没命中任何登记的弹窗：比主平台窗口明显小 → 当「未知弹窗」存证，方便补模板
            area = size[0] * size[1]
            if tuple(size) in KNOWN_SIZES or area >= main_area * 0.9:
                continue
            key = (hwnd, tuple(size))
            if time.time() - _UNK_SEEN.get(key, 0.0) < UNK_GAP:
                continue          # 同一未登记窗口 120s 内不重复存图/记账
            # 窗口刚创建时抓到的是未渲染纯色帧 → 等 0.45s 重抓一次，仍空白才放弃
            if _is_blank(img):
                time.sleep(0.45)
                img2 = grab_hwnd(hwnd)
                if img2 is not None and getattr(img2, "size", 0):
                    img = img2
                if _is_blank(img):
                    if verbose:
                        print(f"   ⚪ 未知窗口 {size[0]}x{size[1]} 还没渲染出内容，跳过存证",
                              flush=True)
                    continue
            _UNK_SEEN[key] = time.time()   # 真正存证才占住 120s，空白帧不占
            try:
                os.makedirs(UNKNOWN_DIR, exist_ok=True)
                out = os.path.join(UNKNOWN_DIR,
                                   time.strftime("popup_unknown_%H%M%S.png"))
                cv2.imencode(".png", img)[1].tofile(out)   # 中文路径只能用 imencode
                if verbose:
                    print(f"   ❔ 未知弹窗「{title}」{size[0]}x{size[1]} → {out}", flush=True)
            except Exception as e:
                out = f"(存图失败 {e!r})"
            hits.append((hwnd, title, size, None, img, (0.0, None, (0, 0, 0, 0))))
    return hits


def close_popups(max_n=3, quiet=False, include_war3=False, cfg_path=DEF_CFG, dry=False):
    """关掉当前弹窗，返回关掉的数量

    2026-11-27 00:10 用户要求：恢复广告弹窗的「点击」关闭（之前 11-26 停用过）。
    """
    popups = load_popups(cfg_path)
    closed = 0
    for _ in range(max(1, max_n)):
        hits = scan_once(popups, include_war3, verbose=not quiet)
        if not hits:
            break
        hwnd, title, size, p, img, m = hits[0]
        if p is None:                     # 未知弹窗：只记不改
            log_event(KIND_UNKNOWN, title, hwnd, size, "记录(未点)",
                      {"note": "尺寸不在已知集合，已存截图待补模板"})
            if not quiet:
                print(f"   ❔ 未登记窗口「{title}」{size[0]}x{size[1]} → 只记录不点", flush=True)
            break
        # 找 X
        cx = cy = None
        ctpl = p.get("close_tpl")
        if ctpl:
            mx = match(img, os.path.join(HERE, ctpl), p.get("close_score", 0.80))
            if mx:
                cx, cy = mx[1]
        if cx is None:
            rx, ry = p.get("close_rel", [0.96, 0.05])
            cx, cy = int(size[0] * rx), int(size[1] * ry)
        ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
        if dry:
            print(f"   [dry] 会点 X @客户区({cx},{cy}) 屏幕({ox + cx},{oy + cy}) "
                  f"窗口 {title} {size[0]}x{size[1]}", flush=True)
            break
        click_screen(hwnd, ox + cx, oy + cy)
        time.sleep(0.9)
        # 复查：弹窗还在不在（窗口可能已经销毁 → 视为已关闭）
        gone = True
        if win32gui.IsWindow(hwnd):
            img2 = grab_hwnd(hwnd)
            if img2 is not None:
                m2 = match_any(img2, p)
                if _hit_ok(img2, m2, p):
                    gone = False
        log_event(p.get("kind", KIND_RANDOM), p["name"], hwnd, size,
                  "已点X关闭" if gone else "已点X(复查仍在)", {"click": [cx, cy]})
        print(f"   🪟 {p.get('kind', KIND_RANDOM)}：{p['name']} → 点 X({cx},{cy}) "
              f"{'已关闭' if gone else '⚠ 复查还在，本轮不再重复点'}", flush=True)
        if gone:
            closed += 1
        break
    return closed

def sweep(seconds=6.0, interval=0.9, max_n=3, quiet=True, include_war3=False,
          cfg_path=DEF_CFG, close_ads=True):
    """在 seconds 秒内持续盯弹窗，来一个关一个

    2026-11-27 00:10 用户要求：恢复广告弹窗的「点击」关闭（之前 11-26 停用过）。
    """
    t_end = time.time() + max(0.0, float(seconds))
    total = 0
    while True:
        if close_ads:
            try:
                import ad_guard
                if ad_guard.close(verbose=False) == "closed":
                    total += 1
            except Exception:
                pass
        try:
            total += close_popups(max_n=max_n, quiet=quiet,
                                  include_war3=include_war3, cfg_path=cfg_path)
        except Exception:
            pass
        left = t_end - time.time()
        if left <= 0:
            break
        time.sleep(min(interval, left))
    if total:
        print(f"   🪟 守候 {seconds:g}s 内清掉 {total} 个弹窗", flush=True)
    return total


def main():
    ap = argparse.ArgumentParser(description="随机弹窗守卫：看到活动的 X 就点掉")
    ap.add_argument("--close", action="store_true", help="关掉当前弹窗")
    ap.add_argument("--scan", action="store_true", help="只扫不点")
    ap.add_argument("--war3", action="store_true", help="连 War3 窗口一起扫")
    ap.add_argument("--max", type=int, default=3, help="最多处理几个弹窗")
    ap.add_argument("--quiet", action="store_true", help="没有弹窗时不输出")
    ap.add_argument("--dry", action="store_true", help="只报告要点的位置，不真点")
    ap.add_argument("--config", default=DEF_CFG)
    ap.add_argument("--check", default=None, help="用一张已有截图跑模板（调试）")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="守候 N 秒，期间出现的弹窗都点掉（退出游戏后用 6）")
    args = ap.parse_args()
    enable_dpi_awareness()

    if args.check:
        img = cv2.imdecode(np.fromfile(args.check, dtype=np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        for p in load_popups(args.config):
            m = match_any(img, p)
            if not m:
                continue
            rx, ry = p.get("close_rel", [0.96, 0.05])
            flags = []
            if m[0] < p.get("score", 0.82):
                flags.append("低于阈值")
            if not _patch_ok(img, m[2], p):
                flags.append("区域不像弹窗")
            tail = f"（{'、'.join(flags)}，不会触发）" if flags else ""
            print(f"{p['name']}: score={m[0]:.3f} @{m[1]} 框={m[2]} "
                  f"→ 按 {w}x{h} 换算 X@{int(w * rx)},{int(h * ry)}{tail}", flush=True)
        return 0

    if args.scan:
        hits = scan_once(load_popups(args.config), args.war3, verbose=True)
        if not hits:
            print("没有发现弹窗", flush=True)
        return 0

    if args.wait and args.wait > 0:
        sweep(args.wait, max_n=args.max, quiet=args.quiet,
              include_war3=args.war3, cfg_path=args.config, close_ads=True)
        return 0

    n = close_popups(args.max, args.quiet, args.war3, args.config, args.dry)
    if n == 0 and not args.quiet:
        print("没有弹窗，什么都没做", flush=True)
    elif n and not args.quiet:
        print(f"已处理 {n} 个弹窗（记录见 logs/popup_events.jsonl）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
