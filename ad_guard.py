# -*- coding: utf-8 -*-
"""随机弹窗守卫：「首充礼包 连锁三天」这类首充/活动广告，以及「216U币 红包」等独立弹窗。

用户 2026-11-17 13:58 反馈：**退出游戏或重开平台时会随机弹这个**，
它会盖住平台主窗（半透明遮罩 + 卡片），把下面所有点击都吃掉
（当天第 1 轮就是被它挡着，点「创建房间」没反应 → 建房子弹窗一直不弹，整轮报废）。

用户要求的处理方式：**直接点右上角 X 关掉**。

两种形态都要认（2026-11-17 20:58 用户又遇到第二种）：
  A. 主窗里的浮层（首充礼包）—— 截图匹配，X 位置按模板偏移算出来
  B. **独立小窗**（「216U币 红包」415x448，标题也是「KK官方对战平台」）
     —— 它是模态的：弹着的时候点「选难度」「开始游戏」全都没反应，
     但视觉上并不遮住按钮，所以以前只表现为「点了没用」。
     它是独立窗口，坐标必须按**它自己的客户区原点**换算成屏幕坐标再点。

判据：卡片头部整块截图（templates/tpl_ad_title.png / tpl_ad_redpacket.png）。
"""
from __future__ import annotations

import os
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")

SCORE = 0.72          # 半分辨率下真弹窗约 0.90~0.99，留足余量
HALF = 2              # 匹配前把截图缩一半：19 尺度全图要 3 秒，缩一半只要 0.3 秒

# 登记过的广告浮层。dx / dy = 「X 中心」相对模板左上角的偏移（按模板宽高比例给，
# 模板被整体缩放时也能算对）。
# 新增一种广告：裁一块**包含 X 本身**的区域存成模板，再把「X 在模板内的像素位置
# ÷ 模板宽高」填进 dx / dy —— 这样 X 的位置由模板自己带出来，不依赖卡片高度。
TPLS = [
    dict(name="首充礼包",
         tpl="templates/tpl_ad_title.png",
         score=SCORE,
         dx=726 / 328, dy=-32 / 52),
    # 2026-11-17 20:58 用户在房间页遇到：「216U币 红包」广告。
    # 它是**独立小窗**（415x448），也是模态的 —— 弹着的时候点「选难度」「开始游戏」
    # 全都没反应，但视觉上并不遮住那些按钮，所以以前只会表现为「点了没用」。
    # 处理办法就是先点右上 X 关掉。
    # 模板裁的是卡片头部（kk官方对战平台 logo + 右上 X + 卡片圆角）；
    # 216U币 那个数字每次不一样，所以故意没裁进来。
    # 2026-11-17 21:2x 实测：模板左上角 = (120,19)，X 中心 = (338,49)
    #   → dx = 218/270，dy = 30/60；close_rel = (0.8145, 0.1094) 相对 415x448 客户区。
    dict(name="红包广告",
         tpl="templates/tpl_ad_redpacket.png",
         score=0.75,
         dx=218 / 270, dy=30 / 60,
         # 光看相似度会误报：logs/plat_1080.png 里有块深色小面板带白 X，能到 0.785。
         # 加一条「命中区域必须是白卡」的硬校验 —— 真弹窗平均亮度 184 / 亮像素 67%，
         # 那块误命中区域是 26 / 0%，一刀就能分开（2026-11-17 21:1x 实测）。
         min_mean=120, min_light=0.40,
         # 只可能是独立小窗：主窗画面里像红包的地方一律不匹配
         # （2026-11-17 22:21 实测主窗一块亮图能到 0.905，点了空处）
         only_small=True),
    # 2026-11-21 02:18 用户要求：新增「我要变强」广告（退出游戏后弹，模态）。
    # 用户定：关闭**优先点「我知道了」**，点不到再点右上 X。
    # 模板裁自实际分辨率截图（弹窗 607x503）：
    #   X 中心相对 (0.9605, 0.0457)；「我知道了」中心相对 (0.5025, 0.9085)
    dict(name="我要变强",
         tpl="templates/tpl_ad_strong.png",
         score=0.80,
         dx=0.9605, dy=0.0457,
         ok_dx=0.5025, ok_dy=0.9085),
]

# 2026-11-26 02:00 现场（v2.3 抓图拍到的 S41「微醺派对」活动弹窗）：
#   它是 Platform.exe **另开的一个无边框小窗**，不是画在主窗里的浮层
#   标题仍然是「KK官方对战平台」，但类名是 Qt5152QWindow（主窗是 Qt5152QWindowIcon）
#   客户区 920x560，屏幕矩形 (500,237)-(1420,797)，完整落在主窗 (294,45)-(1626,990) 内部
#   右上角 X 相对客户区 (899, 24)
# 这种活动弹窗的海报每天都换，做模板必然失效，所以按「窗口几何」认：
#   同进程 + 类名 QWindow 结尾（不是 Icon）+ 客户区 920x560（按 DPI 容差）+ 被主窗包住。
GEOM_POPUPS = [
    dict(name="活动弹窗",
         cls_suffix="QWindow",      # 精确匹配：Qt5152QWindow，排除主窗的 ...QWindowIcon
         base=(920, 560),
         tol=12,                    # 实测正好 920x560；留 12px 给边框/DPI 取整
         rel=(899 / 920, 24 / 560)),
]

_TPL_CACHE = {}


def _load(path):
    """按相对路径加载模板（带缓存）；中文路径只能用 imdecode"""
    if path not in _TPL_CACHE:
        full = os.path.join(HERE, path)
        img = None
        if os.path.exists(full):
            img = cv2.imdecode(np.fromfile(full, dtype=np.uint8), cv2.IMREAD_COLOR)
        _TPL_CACHE[path] = img
    return _TPL_CACHE[path]


def _tpls():
    out = []
    for t in TPLS:
        img = _load(t["tpl"])
        if img is not None:
            d = dict(t)
            d["img"] = img
            out.append(d)
    return out


def _match_multi(img, tpl, lo=0.70, hi=1.60, n=19):
    """多尺度匹配 → (score, scale, (x, y), (w, h))。窗口尺寸/DPI 变化时用得上。"""
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


def _platform_windows(min_w=300, min_h=250):
    """所有平台窗口：主窗 + 独立弹窗窗

    2026-11-17 21:0x 踩坑：原先只收 >=900x600 的窗口，结果「216U币 红包」那种
    **独立小窗**（415x448，标题照样叫「KK官方对战平台」）被排除在外，只能在主窗的
    屏幕截图里看到它 -> 算出来的坐标是主窗客户区坐标 -> 点飞（ad_events 里 action=failed）。
    现在把小窗一起收进来，每个窗口的坐标都按它自己的客户区原点换算成屏幕坐标。
    """
    import win32gui
    import win32process

    out = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
            return
        try:
            title = win32gui.GetWindowText(h) or ""
            cls = win32gui.GetClassName(h) or ""
        except Exception:
            return
        if not cls.startswith("Qt"):
            return
        # 我们自己那个界面也叫 KK…，别扫（扫了只会白花时间）
        if "自动打图" in title:
            return
        # 登录窗不归这里管（login_guard 负责：只停手、不点任何东西）
        if "登录" in title or "登陆" in title:
            return
        try:
            r = win32gui.GetClientRect(h)
            pid = win32process.GetWindowThreadProcessId(h)[1]
        except Exception:
            return
        w, hh = int(r[2]), int(r[3])
        if w < min_w or hh < min_h:
            return
        out.append(dict(hwnd=h, w=w, h=hh, pid=pid, title=title, cls=cls,
                        rect=win32gui.GetWindowRect(h)))

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda x: -(x["w"] * x["h"]))
    return out


def _client_origin(hwnd):
    """窗口客户区左上角的**屏幕坐标**（点击必须用它换算，否则会点到别处）"""
    try:
        import win32gui
        x, y = win32gui.ClientToScreen(hwnd, (0, 0))
        return int(x), int(y)
    except Exception:
        return 0, 0


def _grab_screen_rect(hwnd):
    """按窗口客户区的**屏幕位置**直接抓屏（PrintWindow 抓不到时的退路）"""
    try:
        import win32gui
        from PIL import ImageGrab
        x, y = win32gui.ClientToScreen(hwnd, (0, 0))
        r = win32gui.GetClientRect(hwnd)
        arr = np.array(ImageGrab.grab(bbox=(x, y, x + r[2], y + r[3])))
        if not arr.size:
            return None
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def _match_in(img, tpl_full, score_min):
    """在一张图里找某个模板 → (score, scale, 左上角坐标, (w,h))；不到阈值返回 None

    先按 HALF 缩小粗定位（0.3 秒级），命中再在原图 ROI 里用全尺寸模板精修一次，
    否则点 X 会落到边缘（2026-11-17 14:35 实测：粗算 1084，实际 1098）。
    """
    small = cv2.resize(img, None, fx=1.0 / HALF, fy=1.0 / HALF,
                       interpolation=cv2.INTER_AREA)
    tpl_half = cv2.resize(tpl_full, None, fx=1.0 / HALF, fy=1.0 / HALF,
                          interpolation=cv2.INTER_AREA)
    score, sc, loc, size = _match_multi(small, tpl_half, lo=0.8, hi=1.5, n=9)
    if loc is None or score < score_min:
        return None
    gx, gy = loc[0] * HALF, loc[1] * HALF
    x0 = max(0, gx - 40)
    y0 = max(0, gy - 30)
    x1 = min(img.shape[1], gx + size[0] * HALF + 60)
    y1 = min(img.shape[0], gy + size[1] * HALF + 40)
    roi = img[y0:y1, x0:x1]
    s2, sc2, loc2, size2 = _match_multi(roi, tpl_full, lo=0.95, hi=1.05, n=3)
    if loc2 is not None and s2 >= 0.80:
        return s2, sc2, (loc2[0] + x0, loc2[1] + y0), size2
    return score, sc, (loc[0] * HALF, loc[1] * HALF), (size[0] * HALF, size[1] * HALF)


def _patch_ok(img, loc, size, t):
    """命中区域的「长相」硬校验（可选）：白卡类弹窗要求够亮

    只靠相似度会误报 —— 深色面板上一个白色 X 也能到 0.78。这里按模板登记的
    min_mean（平均亮度）/ min_light（亮像素占比）兜底。
    """
    mm, ml = t.get("min_mean"), t.get("min_light")
    if mm is None and ml is None:
        return True
    x, y = int(loc[0]), int(loc[1])
    w, h = int(size[0]), int(size[1])
    patch = img[max(0, y):y + h, max(0, x):x + w]
    if not getattr(patch, "size", 0):
        return False
    g = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    if mm is not None and g.mean() < mm:
        return False
    if ml is not None and (g > 180).mean() < ml:
        return False
    return True


def _hit_in(img, tpls, w, grab_name, verbose=False):
    """在一张窗口截图里过一遍模板 → hit dict（含客户区坐标 xy 与屏幕坐标 sxy）"""
    win_area = int(w.get("w") or 0) * int(w.get("h") or 0)
    for t in tpls:
        if t.get("only_small") and win_area >= 800000:
            continue      # 只以独立小窗出现的广告：主窗画面不参与匹配
        hit = _match_in(img, t["img"], t["score"])
        if not hit:
            continue
        score, sc, loc, size = hit
        if not _patch_ok(img, loc, size, t):
            if verbose:
                print(f"   （{t['name']} 相似度 {score:.3f} 但区域不像弹窗，跳过）",
                      flush=True)
            continue
        tw, th = size[0], size[1]
        cxy = (int(loc[0] + tw * t["dx"]), int(loc[1] + th * t["dy"]))
        ox, oy = _client_origin(w["hwnd"])
        if verbose:
            print(f"   命中「{t['name']}」score={score:.3f} scale={sc:.2f} "
                  f"窗口 {w['w']}x{w['h']}（{w.get('title', '')}）取图={grab_name} "
                  f"→ 客户区 X@{cxy} 屏幕 X@({cxy[0] + ox},{cxy[1] + oy})", flush=True)
        ok_xy = None
        if t.get("ok_dx") is not None:
            ok_xy = (int(loc[0] + tw * t["ok_dx"]), int(loc[1] + th * t["ok_dy"]))
        return dict(hwnd=w["hwnd"], name=t["name"], score=score, scale=sc,
                    xy=cxy, sxy=(cxy[0] + ox, cxy[1] + oy), ok_xy=ok_xy,
                    chk=loc, size=size, win=(w["w"], w["h"]), grab=grab_name)
    return None


def _geom_popup(wins, verbose=False):
    """按窗口几何认「活动弹窗」（不靠模板，海报每天换）

    条件：比主窗小、类名精确等于 Qt5152QWindow（主窗带 Icon 后缀）、
    客户区 920x560（±12）、且整窗落在主窗矩形内部。
    返回和模板命中同构的 hit dict（xy 是客户区坐标，sxy 是屏幕坐标）。
    """
    if len(wins) < 2:
        return None
    main = wins[0]
    mrect = main.get("rect") or (0, 0, 0, 0)
    for w in wins[1:]:
        for g in GEOM_POPUPS:
            cls = w.get("cls") or ""
            # 精确到「...QWindow」：主窗是 ...QWindowIcon，房间窗等也带 Icon 后缀
            if not cls.endswith(g["cls_suffix"]) or cls.endswith("Icon"):
                continue
            bw, bh = g["base"]
            if abs(w["w"] - bw) > g["tol"] or abs(w["h"] - bh) > g["tol"]:
                continue
            try:
                import win32gui
                style = win32gui.GetWindowLong(w["hwnd"], -16)   # GWL_STYLE
                if style & 0x00C00000:                            # WS_CAPTION：活动弹窗是无边框的
                    continue
            except Exception:
                pass
            r = w.get("rect") or (0, 0, 0, 0)
            inside = (r[0] >= mrect[0] - 8 and r[1] >= mrect[1] - 8
                      and r[2] <= mrect[2] + 8 and r[3] <= mrect[3] + 8)
            if not inside:
                continue
            ox, oy = _client_origin(w["hwnd"])
            cxy = (int(w["w"] * g["rel"][0]), int(w["h"] * g["rel"][1]))
            if verbose:
                print(f"   按几何命中「{g['name']}」窗口 {w['w']}x{w['h']} "
                      f"rect={tuple(r)} 类={w.get('cls')} "
                      f"→ 客户区 X@{cxy} 屏幕 X@({cxy[0] + ox},{cxy[1] + oy})",
                      flush=True)
            return dict(hwnd=w["hwnd"], name=g["name"], score=1.0, scale=1.0,
                        xy=cxy, sxy=(cxy[0] + ox, cxy[1] + oy), ok_xy=None,
                        chk=None, size=(w["w"], w["h"]),
                        win=(w["w"], w["h"]), grab="几何")
    return None


def find(verbose=False):
    """返回 {'hwnd','name','score','scale','xy':(客户区),'sxy':(屏幕),'chk'}；没有 None

    顺序：先按窗口几何认活动弹窗（最快、不怕换海报）；没有再过模板；
    模板顺序：先把每个窗口自己的画面过一遍（PrintWindow，被遮挡也能看）；
    都不中再只对**最大那个窗**（主平台窗）做一次屏幕抓取兜底 —— 独立弹窗浮在主窗
    上面时屏幕视角能看到它，坐标用主窗客户区原点换算成屏幕坐标。
    """
    tpls = _tpls()
    if not tpls and not GEOM_POPUPS:
        return None
    try:
        import popup_guard as pg
    except Exception:
        return None
    wins = _platform_windows()
    hit = _geom_popup(wins, verbose)
    if hit:
        return hit
    if not tpls:
        return None
    for w in wins:
        img = pg.grab_hwnd(w["hwnd"])
        if img is None or not getattr(img, "size", 0):
            continue
        hit = _hit_in(img, tpls, w, "窗口", verbose)
        if hit:
            return hit
    for w in wins[:1]:
        img = _grab_screen_rect(w["hwnd"])
        if img is None or not getattr(img, "size", 0):
            continue
        hit = _hit_in(img, tpls, w, "屏幕", verbose)
        if hit:
            return hit
    return None


def _shot(hwnd, name):
    try:
        import popup_guard as pg
        img = pg.grab_hwnd(hwnd)
        if img is None or not getattr(img, "size", 0):
            return None
        os.makedirs(LOG_DIR, exist_ok=True)
        p = os.path.join(LOG_DIR, name)
        cv2.imencode(".png", img)[1].tofile(p)
        return p
    except Exception:
        return None


def _click(hwnd, cxy):
    """按客户区坐标点一下（内部换算成屏幕坐标）"""
    import popup_guard as pg
    ox, oy = _client_origin(hwnd)
    pg.click_screen(hwnd, cxy[0] + ox, cxy[1] + oy)


def _alive(hwnd):
    try:
        import win32gui
        return bool(hwnd) and bool(win32gui.IsWindow(hwnd))
    except Exception:
        return False


def _send_esc(hwnd):
    """给窗口发一次 ESC（用户 2026-11-26：点完 X 再按一下 ESC 才算彻底清干净）"""
    if not hwnd:
        return False
    try:
        import win32gui
        import plat_restart as pr
        win32gui.PostMessage(hwnd, pr.WM_KEYDOWN, pr.VK_ESCAPE, 0)
        win32gui.PostMessage(hwnd, pr.WM_KEYUP, pr.VK_ESCAPE, 0)
        return True
    except Exception:
        return False


def _esc_after_close(hit, main_hwnd):
    """关掉弹窗后补一次 ESC —— 所有弹窗都补（用户 2026-11-26）

    弹窗窗还活着就直接发给它；已经销毁就发给平台主窗（残留的遮罩层挂在主窗上）。
    主窗在前台时才按：房间里/游戏里前台不是主窗，乱按 ESC 可能退出界面。
    """
    tgt = hit["hwnd"] if _alive(hit["hwnd"]) else main_hwnd
    if not tgt:
        return False
    if tgt == main_hwnd:
        try:
            import win32gui
            fg = win32gui.GetForegroundWindow()
            if fg and fg != main_hwnd and _alive(fg):
                return False
        except Exception:
            pass
    ok = _send_esc(tgt)
    time.sleep(0.5)
    return ok


def close(verbose=True, tries=2):
    """先点击、点不掉才补一次 ESC。返回 'closed' / 'none' / 'failed'

    2026-11-27 00:10 用户要求：**恢复之前的「点击」关闭**（广告巡检重新启用）。
    2026-11-27 00:14 用户明确顺序：**先点击，两次都点不掉才补一次 ESC**。
      ① 我要变强那种有「我知道了」→ 先点它（最多 2 次）
      ② 点右上角 X（最多 2 次，每次偏 4/6px 修正）
      ③ 上面全部失败 → 才补一次 ESC
    """
    hit = find(verbose=verbose)
    if not hit:
        return "none"
    p = _shot(hit["hwnd"], "ad_popup.png")
    if verbose:
        print(f"   {hit.get('name', '广告弹窗')}截图：{p}", flush=True)
    # 2026-11-21 02:18 用户定：「我要变强」优先点「我知道了」，点不到再点右上 X
    ok = hit.get("ok_xy")
    if ok:
        for k in range(1, tries + 1):
            try:
                _click(hit["hwnd"], ok)
            except Exception as e:
                if verbose:
                    print(f"   第 {k} 次点「我知道了」出错：{type(e).__name__}: {e}", flush=True)
            time.sleep(0.9)
            if not find(verbose=False):
                if verbose:
                    print(f"   ✓ 广告弹窗「{hit.get('name', '')}」已关掉（第 {k} 次点「我知道了」生效）",
                          flush=True)
                _log(hit, "closed", k)
                return "closed"
            ok = (ok[0], ok[1] + 6)
    for k in range(1, tries + 1):
        try:
            _click(hit["hwnd"], hit["xy"])
        except Exception as e:
            if verbose:
                print(f"   第 {k} 次点 X 出错：{type(e).__name__}: {e}", flush=True)
        time.sleep(0.9)
        if not find(verbose=False):
            if verbose:
                print(f"   ✓ 广告弹窗「{hit.get('name', '')}」已关掉（第 {k} 次点 X 生效）",
                      flush=True)
            _log(hit, "closed", k)
            return "closed"
        # 间距/DPI 有偏差 → 向下 6px、向右 4px 再试一次
        hit["xy"] = (hit["xy"][0] + 4, hit["xy"][1] + 6)
    # 点击全部失败（点 2 次都没关掉）→ 才补这一次 ESC
    if verbose:
        print(f"   ⚠ 点 X {tries} 次都没关掉 → 补一次 ESC", flush=True)
    if _send_esc(hit["hwnd"]):
        time.sleep(0.8)
        if not find(verbose=False):
            if verbose:
                print("   ✓ 按 ESC 关掉了广告弹窗", flush=True)
            _log(hit, "closed_esc", tries)
            return "closed"
    _log(hit, "failed", tries)
    return "failed"


def _log(hit, action, tries):
    try:
        import json
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = dict(ts=time.strftime("%Y-%m-%d %H:%M:%S"), action=action,
                   name=hit.get("name", ""),
                   score=round(hit["score"], 3), scale=round(hit["scale"], 2),
                   xy=list(hit["xy"]), sxy=list(hit.get("sxy") or ()),
                   win=list(hit.get("win") or ()), grab=hit.get("grab", ""),
                   tries=tries)
        with open(os.path.join(LOG_DIR, "ad_events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def check(verbose=False):
    """有就关掉；返回是否关过（给 pipeline/重启流程当护栏用）"""
    r = close(verbose=verbose)
    return r == "closed"


if __name__ == "__main__":
    import sys
    if "--scan" in sys.argv:
        print(find(verbose=True) or "没有首充/活动/红包弹窗")
    elif "--windows" in sys.argv:              # 看看都枚举到了哪些平台窗
        for w in _platform_windows():
            print(f"hwnd={w['hwnd']} {w['w']}x{w['h']} pid={w['pid']} 「{w['title']}」")
    elif "--check" in sys.argv:                 # 用已有截图调试模板
        i = sys.argv.index("--check")
        shot = sys.argv[i + 1]
        img = cv2.imdecode(np.fromfile(shot, dtype=np.uint8), cv2.IMREAD_COLOR)
        for t in _tpls():
            hit = _match_in(img, t["img"], 0.0)
            if not hit:
                print(f"{t['name']}: 未命中")
                continue
            score, sc, loc, size = hit
            xy = (int(loc[0] + size[0] * t["dx"]), int(loc[1] + size[1] * t["dy"]))
            ok = _patch_ok(img, loc, size, t)
            flag = "" if ok else f"（低于阈值 {t['score']} 或区域不像弹窗，不会触发）"
            print(f"{t['name']}: score={score:.3f} scale={sc:.2f} 左上={loc} → X@{xy}{flag}",
                  flush=True)
    else:
        print(close(verbose=True))
