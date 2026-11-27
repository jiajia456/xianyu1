# -*- coding: utf-8 -*-
"""
房间窗口识别 + 「您正在房间内…」弹窗处理

背景（用户 01:18 说明）：
  · 平台建房成功后，房间是一个**独立的顶层窗口**（同进程，尺寸 1224x904），
    主平台窗口仍在「地图详情页」，看起来像没建房。
  · 在房间里**重复建房/重复切换地图**，主窗口会弹「平台提示：您正在房间内，
    如果继续切换，将退出房间，确定继续吗？」
      - 「确定」= 真的退出房间（房间号作废）
      - 「取消(3)」= 留在房间
  · pipeline 的建房流程遇到该提示会点「确定」，退出旧房间后重新建房。

用法：
  python room_finder.py --list           # 列出所有平台窗口并判定 主窗/房间
  python room_finder.py --find           # 找房间窗口，打印 hwnd（找不到 exit=1）
  python room_finder.py --popup          # 只在**前台窗口**里找该弹窗
  python room_finder.py --popup --all    # 遍历所有平台窗口找弹窗
  python room_finder.py --dismiss        # 找到弹窗就点「取消」
  python room_finder.py --dismiss --confirm   # 点「确定」（会退出房间）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import cv2
import numpy as np
import win32api
import win32con
import win32gui
import win32process
from PIL import ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(HERE, "templates")

CANCEL_RATIO = 1.122      # 取消按钮中心相对「确定」中心的横向偏移（× 确定按钮宽度）
ROOM_SCORE = 0.85         # 「开始游戏」模板命中阈值
CREATE_SCORE = 0.75       # 「创建房间」模板命中阈值
WARNPAIR_SCORE = 0.72     # 「确定」+「取消(3)」成对模板命中阈值（仅参考，区分力不够）
WARNLBL_SCORE = 0.80      # 「平台提示」标题模板阈值：真弹窗 1.000 / 建房对话框 0.34


# ---------------------------------------------------------------- 模板
def _tpl(name):
    p = os.path.join(TPL_DIR, name)
    if not os.path.exists(p):
        return None
    # Windows 下 cv2.imread 读不了含中文的路径，用 fromfile 绕过
    data = np.fromfile(p, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


_TPL_START = _tpl("btn_start_game.png")
_TPL_START2 = _tpl("btn_start_game_skin2.png")   # 第二套皮肤（金边按钮）
_TPL_START3 = _tpl("btn_start_game_skin3.png")   # 第三套皮肤（水墨风房间窗）
_TPL_CREATE = _tpl("btn_create_room.png")
_TPL_ROOMNO = _tpl("lbl_room_no.png")     # 房间窗口左上「房间号」三个字（主窗口 0.70 / 房间 1.00）
_TPL_ROOMNO3 = _tpl("lbl_room_no_skin3.png")  # 水墨皮肤版「房间号：199465」整行（房间 1.000 / 主窗 0.521）
_TPL_WARNPAIR = _tpl("btn_platform_confirm.png")  # 「确定」+「取消(3)」成对按钮
_TPL_WARNLBL = _tpl("lbl_platform_warn.png")      # 弹窗里的「平台提示」标题文字：唯一可靠的区分特征

# 房间信息校验（房间名 / 密码）用；全部以「房间号」标签为锚点，靠固定偏移取样，
# 这样房间窗口尺寸变化时也不会串到别的行。
_TPL_RNAME = _tpl("lbl_room_name.png")       # 「房间名」标签（地图详情页的「房间列表」表头也有，必须限定区域用）
_TPL_RNAME_OK = _tpl("tpl_room_name_ok.png")  # 房间名框里的「我在峡谷打」
_TPL_RPWD = _tpl("lbl_room_pwd2.png")        # 房间里的「密 码」标签
_TPL_RQUIT = _tpl("btn_room_quit.png")       # 房间右下「退出」按钮
_TPL_LEAVECFM = _tpl("lbl_leave_confirm.png")  # 「是否确认退出房间？」文字（房间内 0.31）

ROOMNO_SCORE = 0.90       # 房间号标签阈值（房间 1.000 / 地图详情页 0.698）
NAME_OK_SCORE = 0.80      # 房间名文字「我在峡谷打」阈值（房间内 1.000 / 别处 0.787）
QUIT_SCORE = 0.86         # 「退出」按钮阈值（房间内 1.000 / 地图详情页 0.62）
START_SCORE = 0.72        # 「开始游戏」按钮亮着（可开局）的阈值（灰掉/游戏中 <0.72）
LEAVECFM_SCORE = 0.85     # 「是否确认退出房间？」阈值（弹窗上 1.000 / 房间内 0.31）
LAYOUT_SCORE = 0.80       # 「房间名」标签版面自检阈值（正常 1.000；版面不符就别乱判）
PWD_MIN_WHITE = 20        # 密码框内近白像素下限（有密码 108px / 无密码 0px）

# 相对「房间号」标签**中心**的偏移（1224x904 房间窗口实测；_score 返回的就是模板中心）
OFF_NAME_LBL = (-19, 270)  # 「房间名」标签中心（只用来做版面自检）
OFF_NAME_VAL = (61, 269)   # 房间名文字中心
OFF_PWD_BOX = (91, 303)    # 密码输入框中心
OFF_QUIT = (938, 501)      # 「退出」按钮中心


def _score(img, tpl):
    if tpl is None or img is None:
        return 0.0, None
    if img.shape[0] < tpl.shape[0] or img.shape[1] < tpl.shape[1]:
        return 0.0, None
    res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, ml = cv2.minMaxLoc(res)
    return float(mx), (ml[0] + tpl.shape[1] // 2, ml[1] + tpl.shape[0] // 2)


# ---------------------------------------------------------------- 窗口枚举
def pid_to_exe(pid):
    try:
        import psutil  # noqa
        return psutil.Process(pid).name()
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        ctypes.windll.kernel32.CloseHandle(h)
        return os.path.basename(buf.value)
    except Exception:
        return ""


def normal_rect(h):
    """最小化窗口的「还原后」尺寸。

    最小化时 GetClientRect 返回 0x0、GetWindowRect 返回 -32000，
    只有 GetWindowPlacement 的 rcNormalPosition 还留着真实尺寸。
    """
    import ctypes

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                    ("showCmd", ctypes.c_uint), ("ptMinPosition", POINT),
                    ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]

    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    try:
        ctypes.windll.user32.GetWindowPlacement(h, ctypes.byref(wp))
        r = wp.rcNormalPosition
        return (r.l, r.t, r.r, r.b)
    except Exception:
        return (0, 0, 0, 0)


def platform_windows(min_client=500, include_iconic=True):
    """所有可见的平台窗口 → [{hwnd,pid,title,rect,client,iconic}]

    include_iconic=True 时把「最小化的平台窗口」也算进来：
    实测点「开始游戏」后房间窗口不会消失，而是被最小化（IsIconic=1、rect=-32000），
    旧逻辑直接跳过它 → 误判「没有房间」→ 又去建房 → 撞上「您正在房间内…」弹窗。
    """
    raw = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        iconic = bool(win32gui.IsIconic(h))
        if iconic and not include_iconic:
            return
        r = win32gui.GetWindowRect(h)
        if iconic:
            nr = normal_rect(h)                      # 最小化：只能用还原尺寸
            c = (nr[0], nr[1], nr[2] - nr[0], nr[3] - nr[1])
        else:
            c = client_rect(h)
        if c[2] < min_client or c[3] < min_client:
            return
        raw.append((h, win32process.GetWindowThreadProcessId(h)[1], r, c, iconic))

    win32gui.EnumWindows(cb, None)

    out = []
    for h, pid, r, c, iconic in raw:
        exe = (pid_to_exe(pid) or "").lower()
        if "platform" not in exe:
            continue
        out.append({"hwnd": h, "pid": pid, "title": win32gui.GetWindowText(h),
                    "rect": r, "client": c, "exe": exe, "iconic": iconic})
    return out


def client_rect(h):
    import ctypes
    from ctypes import wintypes

    class RC(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    rc = RC()
    ctypes.windll.user32.GetClientRect(h, ctypes.byref(rc))
    pt = wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(h, ctypes.byref(pt))
    return (pt.x, pt.y, rc.r, rc.b)


def grab_client(h, raise_win=True, settle=0.55):
    """截客户区。平台窗口被别的窗口遮挡时要先置顶（不抢焦点）。"""
    c = client_rect(h)
    if c[2] < 100 or c[3] < 100:
        return None
    if raise_win:
        win32gui.SetWindowPos(h, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                              win32con.SWP_NOACTIVATE)
        time.sleep(settle)
    try:
        img = np.array(ImageGrab.grab(bbox=(c[0], c[1], c[0] + c[2], c[1] + c[3])))
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    except Exception:
        img = None
    if raise_win:
        win32gui.SetWindowPos(h, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                              win32con.SWP_NOACTIVATE)
    return img


# ---------------------------------------------------------------- 房间判定
def _best(img, *tpls):
    """多套皮肤模板取最高分。模板缺失(None)按 0 处理。"""
    best = 0.0
    for t in tpls:
        sc, _ = _score(img, t)
        if sc > best:
            best = sc
    return best


def is_room(img):
    """房间窗口判据：房间号 + 开局按钮或退出按钮。

    实测分离度：房间窗 roomno=1.000 / start=0.991；主平台窗 roomno=0.698；建房对话框 0.672。
    单看按钮会被同款蓝色按钮带偏（create 两个窗口都是 0.905），所以以「房间号」为准。

    房间进入「游戏中」时，「开始游戏」会变成深色「游戏中」，旧判据因此把
    明明存在的房间认成「没有房间」，继而重复搜图/建房。此时右下角「退出」
    仍在；用它作辅助识别，但不把「游戏中」误当成可再次开局。
    """
    s_no = _best(img, _TPL_ROOMNO, _TPL_ROOMNO3)
    s_start = _best(img, _TPL_START, _TPL_START2, _TPL_START3)
    s_quit = _best(img, _TPL_RQUIT)
    return (s_no >= ROOMNO_SCORE and
            (s_start >= START_SCORE or s_quit >= QUIT_SCORE)), s_no, s_start


def is_room_busy(room):
    """已识别房间，但「开始游戏」不可用、只剩「退出」时不再尝试开局。"""
    return (room is not None and room.get("start_score") is not None
            and room["start_score"] < START_SCORE
            and room.get("quit_score", 0) >= QUIT_SCORE)


def find_room_by_hwnd(hwnd, activate_first=True):
    """看一眼指定房间窗（不改前后顺序用 peek_window，拿不到再 activate 后抓）。

    给「断线重连弹窗收掉后，房间「开始游戏」还亮不亮」用。
    返回 (房间号分, 开始游戏分, 创建房间按钮分) 或 None（完全拿不到画面）。
    """
    if not hwnd:
        return None
    try:
        pk = peek_window(hwnd)
    except Exception:
        pk = None
    if pk is not None:
        return pk
    if not activate_first:
        return None
    try:
        activate(hwnd, settle=0.6)
        return peek_window(hwnd)
    except Exception:
        return None


def _grab_ready(h, tries=3, wait=0.6):
    """还原/抢前台之后截图，等窗口真的画出来再判。

    踩过的坑：SW_RESTORE 返回不代表窗口已经画好了，立刻截图会截到桌面或别的窗口，
    房间号模板只有 0.2~0.4 分 → 误判「没有房间」→ 又去建房 → 连环失败。
    所以这里连截几次，取「房间号得分最高」的那张。
    """
    best, best_img = -1.0, None
    for i in range(tries):
        r = win32gui.GetWindowRect(h)
        if r[0] < -10000 or r[1] < -10000:      # 还在最小化位置，再等等
            time.sleep(wait)
            continue
        img = grab_client(h)
        if img is not None and img.size:
            s, _ = _score(img, _TPL_ROOMNO)
            if s > best:
                best, best_img = s, img
            if s >= 0.90:                        # 已经是房间画面，够用了
                return best_img
        time.sleep(wait)
    return best_img


def find_room(verbose=True, restore=True):
    """返回第一个房间窗口信息 dict；找不到 None

    restore=True：房间窗口被最小化时先还原再判定（最小化窗口的客户区坐标是 -32000，
    直接截图会截到桌面垃圾画面）。房间号模板在还原后是 1.000，所以判定很稳。
    """
    for w in platform_windows():
        restored = False
        if w.get("iconic"):
            if not restore:
                if verbose:
                    print(f"   hwnd={w['hwnd']} client={w['client'][2]}x{w['client'][3]} 处于最小化 → 跳过判定"
                          f"（要判定请去掉 restore=False）", flush=True)
                continue
            if verbose:
                print(f"   房间窗口 hwnd={w['hwnd']} 处于最小化 → 先还原", flush=True)
            activate(w["hwnd"], settle=0.45)
            w["iconic"] = False
            restored = True
        img = _grab_ready(w["hwnd"]) if restored else grab_client(w["hwnd"])
        if img is None:
            continue
        ok, s1, s2 = is_room(img)
        if verbose:
            print(f"   hwnd={w['hwnd']} client={w['client'][2]}x{w['client'][3]} "
                  f"房间号={s1:.3f} 开始游戏={s2:.3f} → {'房间 ✔' if ok else '非房间'}", flush=True)
        if ok:
            w["img"] = img
            w["score"] = s1
            w["start_score"] = s2
            w["quit_score"] = _best(img, _TPL_RQUIT)
            return w
    return None


def peek_window(hwnd):
    """不改窗口前后顺序地看一眼这扇窗的内容（PrintWindow 优先；W7 黑图时
    popup_guard 会自动退屏幕抓取 —— 新出现的窗口通常正盖在最上层，也能拍对）。

    给「按新窗口绑房间」做确认用（2026-11-26 18:57 用户定的双保险方案）：
      · s_create ≥ 0.80 → 这扇窗里还有「创建房间」按钮 = 详情页，建房没成，不能当房间；
      · s_no / s_start 达标 → 就是房间；
      · 都不高 → W7 渲染慢还没画出来，由调用方决定信不信。
    返回 (房间号分, 开始游戏分, 创建房间按钮分)；完全拿不到画面返回 None。
    """
    try:
        import popup_guard as pg
        img = pg.grab_hwnd(hwnd)
    except Exception:
        img = None
    if img is None or not getattr(img, "size", 0):
        return None
    s_no = _best(img, _TPL_ROOMNO, _TPL_ROOMNO3)
    s_start = _best(img, _TPL_START, _TPL_START2, _TPL_START3)
    s_create = 0.0
    if _TPL_CREATE is not None:
        s_create = _score(img, _TPL_CREATE)[0]
    return s_no, s_start, s_create


def is_room_window(h, verbose=False):
    """当前这个窗口是不是房间窗（只识别，不置顶、不抢前台）。

    2026-11-26 22:37 Win7 实测：正常退出游戏后平台回到房间窗（1224x904），
    紧接着「局间 ESC ×2」绑到的就是这个窗口 —— 房间窗上的 ESC = 退出房间
    （第 1 次弹「是否确认退出房间？」，5 秒后第 2 次确认），8 秒后房间窗就没了，
    下一轮判「没有房间」→ 搜地图建房 → 房间⇄详情页死循环。
    ESC 前先用它认一下：是房间窗就别按，房间保住下一轮直接开局。
    """
    if not h:
        return False
    try:
        if not win32gui.IsWindow(h):
            return False
    except Exception:
        return False
    try:
        import popup_guard as pg
        img = pg.grab_hwnd(h)          # PrintWindow 优先，W7 黑图会自动退屏幕抓取
    except Exception:
        img = None
    if img is None or not getattr(img, "size", 0):
        return False
    ok, s_no, s_start = is_room(img)
    if verbose:
        print(f"   房间窗识别：hwnd={h} 房间号={s_no:.3f} 开始游戏={s_start:.3f}"
              f" → {'房间 ✔' if ok else '不是房间'}", flush=True)
    return ok


def top_window_at(h, cx, cy):
    """客户区坐标 (cx,cy) 处最上层的顶层窗口 hwnd。

    给「点创建房间前先确认没被房间窗盖住」用：纯几何判断，不依赖模板分数
    （W7 上房间画面还没刷新时模板会判不出，但窗口位置不会骗人）。
    """
    try:
        o = client_rect(h)
        w = win32gui.WindowFromPoint((o[0] + int(cx), o[1] + int(cy)))
        if not w:
            return 0
        return win32gui.GetAncestor(w, 2) or w   # GA_ROOT
    except Exception:
        return 0


def has_create_room_btn(threshold=0.80, verbose=False):
    """屏幕上还有「创建房间」按钮吗（= 平台仍停在「地图详情」页）。**只识别，不点击。**

    实测分数：详情页 0.905（W7 0.842）/ 房间页 0.412。
    用途：「您正在房间内…？」弹窗点「确定」退房之后判断下一步 ——
      在   → 跳过 ① 搜地图，直接回 ② 重建房间；
      不在 → 详情页也丢了，只能整段从 ① 重来。
    返回 (是否命中, 最高分)。
    """
    best = 0.0
    for w in platform_windows(min_client=300):
        if w.get("iconic"):
            continue
        try:
            info = peek_window(w["hwnd"])
        except Exception:
            info = None
        if info is None:
            continue
        s_create = float(info[2])
        if verbose:
            print(f"   详情页探测：hwnd={w['hwnd']} {w['client'][2]}x{w['client'][3]} "
                  f"「创建房间」score={s_create:.3f}", flush=True)
        if s_create > best:
            best = s_create
        if s_create >= threshold:
            return True, s_create
    return False, best


def warn_leave(dry=False, verbose=True):
    """「平台提示：您正在房间内…？」弹窗 → 点「确定」→ 退出房间（用户 2026-11-26 定）。

    和 `handle_popup(prefer_cancel=True)` 相反：这里**就是要解散房间重新建**。
    点「确定」之后平台会自己把房间关掉，`leave_room()` 这时通常只剩兜底
    （点「退出」→「是否确认退出房间？」点确定），房间真没了会直接返回 no_room。

    返回：
      'left'       已退干净（房间窗探不到了）
      'room_still' 房间还在（没退掉 → 调用方要整段重来，别再拿它开局）
      'no_popup'   本来就没有这个弹窗
      'dry'        只探测
    """
    if dry:
        r = handle_popup(prefer_cancel=False, scan_all=True, dry=True, verbose=False)
        return "found" if r == "found" else "no_popup"
    r = handle_popup(prefer_cancel=False, scan_all=True, verbose=verbose)
    if r != "clicked":
        return "no_popup"
    if verbose:
        print("   已点「确定」（房间将被解散）", flush=True)
    time.sleep(1.5)
    try:
        lr = leave_room(verbose=verbose, tries=2)
    except Exception as e:
        lr = f"error:{e!r}"
    if verbose:
        print(f"   退出房间结果：{lr}", flush=True)
    try:
        if find_room(verbose=False, restore=False) is not None:
            return "room_still"
    except Exception:
        pass
    return "left"


# ---------------------------------------------------------------- 房间信息（房间名 / 密码）
def _region_around(cx, cy, w, h, W, H):
    return (max(0, cx - w // 2), max(0, cy - h // 2),
            min(W, cx + w // 2), min(H, cy + h // 2))


def room_info(img, verbose=False):
    """读房间画面上的「房间名 / 密码」。

    返回 {name_score, name_ok, pwd_white, pwd_dots, pwd_ok}；不是房间画面则返回 None。

    做法：先认「房间号」标签（房间内 1.000 / 地图详情页 0.698，唯一稳定锚点），
    再按实测偏移取「房间名」文字框和「密码」输入框。
      · 房间名：和「我在峡谷打」模板比对（房间内 1.000 / 地图详情页 0.787）
      · 密码：数输入框里的近白像素 —— 有密码时是 2 个 8x8 圆点（108px），空密码是 0px
    """
    if img is None or not getattr(img, "size", 0):
        return None
    s_no, no = _score(img, _TPL_ROOMNO)
    # 新版水墨皮肤：旧锚点偏移（OFF_NAME_*/OFF_PWD_BOX）全都对不上，硬读会得出
    # 「名字不是我在峡谷打 / 密码没设」→ verify_room_before_start 把好房间退掉重建。
    # 所以认出新皮肤就直接返回 None，调用方 ok_to_start(None)=True，按原样继续。
    s_no3, _ = _score(img, _TPL_ROOMNO3)
    if s_no3 >= ROOMNO_SCORE and s_no3 > s_no:
        if verbose:
            print(f"   房间信息：新版皮肤（房间号 score={s_no3:.3f}）→ 版面偏移未适配，跳过校验", flush=True)
        return None
    if no is None or s_no < ROOMNO_SCORE:
        return None
    H, W = img.shape[:2]

    # 版面自检：锚点偏移处必须能找到「房间名」标签。找不到说明版面变了（平台改版/尺寸变了），
    # 这时「房间名不对」是不可信的 —— 宁可返回 None 让调用方"不拦"，也别把好房间退掉。
    lx, ly = no[0] + OFF_NAME_LBL[0], no[1] + OFF_NAME_LBL[1]
    bx0, by0, bx1, by1 = _region_around(lx, ly, 41 + 26, 20 + 20, W, H)
    s_lbl, _ = _score(img[by0:by1, bx0:bx1], _TPL_RNAME)
    if s_lbl < LAYOUT_SCORE:
        if verbose:
            print(f"   房间信息：版面自检失败（「房间名」标签 score={s_lbl:.3f}）→ 无法判定，按原样继续",
                  flush=True)
        return None

    # 房间名：在值位置附近留 ±14px 余量找「我在峡谷打」
    vx, vy = no[0] + OFF_NAME_VAL[0], no[1] + OFF_NAME_VAL[1]
    x0, y0, x1, y1 = _region_around(vx, vy, 77 + 28, 23 + 20, W, H)
    s_name, _ = _score(img[y0:y1, x0:x1], _TPL_RNAME_OK)

    # 密码：框内近白像素 + 连通域（圆点直径约 8px）
    px, py = no[0] + OFF_PWD_BOX[0], no[1] + OFF_PWD_BOX[1]
    bx0, by0, bx1, by1 = _region_around(px, py, 138, 30, W, H)
    reg = img[by0:by1, bx0:bx1]
    white_px, dots = 0, 0
    if reg.size:
        g = cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY)
        white_px = int((g > 200).sum())
        if white_px >= 6:
            n, _, st, _ = cv2.connectedComponentsWithStats((g > 200).astype(np.uint8), 8)
            dots = sum(1 for i in range(1, n) if st[i][4] >= 12)

    info = {
        "name_score": round(float(s_name), 3),
        "name_ok": bool(s_name >= NAME_OK_SCORE),
        "pwd_white": white_px,
        "pwd_dots": int(dots),
        "pwd_ok": bool(white_px >= PWD_MIN_WHITE and dots >= 1),
        "anchor": no,
    }
    if verbose:
        print(f"   房间信息：房间名 {'✔ 我在峡谷打' if info['name_ok'] else '✘ 不是「我在峡谷打」'}"
              f"（score={info['name_score']:.3f}）"
              f" | 密码 {'✔ 已设置' if info['pwd_ok'] else '✘ 未设置'}"
              f"（白点 {white_px}px / {dots} 个）", flush=True)
    return info


def check_room(hwnd=None, verbose=True, restore=False):
    """找房间（或指定 hwnd）并读房间名/密码。返回 (hwnd, info) 或 (None, None)"""
    if hwnd is None:
        room = find_room(verbose=verbose, restore=restore)
        if not room:
            return None, None
        hwnd = room["hwnd"]
        img = room.get("img")
        if img is None or not getattr(img, "size", 0):
            img = grab_client(hwnd)
    else:
        img = grab_client(hwnd)
    info = room_info(img, verbose=verbose)
    return hwnd, info


def ok_to_start(info):
    """开局前放行条件：名字对 + 密码已设。info 为 None（读不出来）时按"不拦"处理，
    避免因为一次截图失败就把好房间反复解散重建。"""
    if info is None:
        return True
    return bool(info["name_ok"] and info["pwd_ok"])


def detect_leave_confirm(img):
    """找「平台提示：是否确认退出房间？」确认框。

    返回 (confirm_center, label_center, score)；没有则 None。
    「确定」是蓝色、「取消(n)」是深灰，所以在文字下方找蓝色连通域，取离文字中心最近的那个。
    ⚠️ 这个框带倒计时（取消(4)），几秒后自动取消 —— 必须尽快点。
    """
    if img is None or not getattr(img, "size", 0):
        return None
    s, pt = _score(img, _TPL_LEAVECFM)
    if pt is None or s < LEAVECFM_SCORE:
        return None
    H, W = img.shape[:2]
    x0, x1 = max(0, pt[0] - 220), min(W, pt[0] + 260)
    y0, y1 = max(0, pt[1] + 20), min(H, pt[1] + 150)
    reg = img[y0:y1, x0:x1]
    if not reg.size:
        return None
    b, g, r = reg[:, :, 0].astype(int), reg[:, :, 1].astype(int), reg[:, :, 2].astype(int)
    mask = ((b > 170) & (r < 115) & (g > 85) & (g < 195)).astype(np.uint8)
    if mask.sum() < 800:
        return None
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    best = None
    for i in range(1, n):
        bx, by, bw, bh, area = st[i]
        if area < 1200 or bw < 90:
            continue
        if not (2.2 <= bw / max(bh, 1) <= 6.5):
            continue
        cx, cy = int(x0 + bx + bw // 2), int(y0 + by + bh // 2)
        d = abs(cx - pt[0])
        if best is None or d < best[0]:
            best = (d, cx, cy)
    if best is None:
        return None
    return (best[1], best[2]), pt, float(s)


def _click_quit(hwnd, pt):
    """点房间右下角「退出」（客户区坐标）。用强前台 + 屏幕坐标，别被前台锁吞掉。"""
    try:
        c = client_rect(hwnd)
        import popup_guard as _pg
        _pg.click_screen(hwnd, c[0] + int(pt[0]), c[1] + int(pt[1]))
    except Exception:
        click_xy(hwnd, *pt)


def find_leave_confirm_any(hwnd=None):
    """在**所有**可见 Qt 窗口里找「是否确认退出房间？」。

    实测（2026-11-17 11:48）：这个确认框有时画在房间窗里，有时是**独立的 480x300 弹窗**
    （标题「平台提示」，确定 / 取消(1)，只挂 1~4 秒就自己取消）。
    老实现只在房间窗里找，弹窗是独立窗口时永远等不到 → 一直重试「退出」→ 退出不了。

    返回 (hwnd, (cx, cy)) 或 None
    """
    import win32gui

    tpl = _TPL_LEAVECFM
    if tpl is None:
        return None
    cands = []
    if hwnd:
        cands.append(hwnd)

    def cb(h, _):
        if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
            return
        try:
            cls = win32gui.GetClassName(h) or ""
        except Exception:
            return
        if cls.startswith("Qt"):
            cands.append(h)

    win32gui.EnumWindows(cb, None)

    for h in cands:
        try:
            c = client_rect(h)
        except Exception:
            continue
        if c[2] < 200 or c[3] < 120:
            continue
        img = grab_client(h, raise_win=True, settle=0.18)
        if img is None or not getattr(img, "size", 0):
            continue
        s, pt = _score(img, _TPL_LEAVECFM)
        if pt is None or s < LEAVECFM_SCORE:
            continue
        hit = detect_leave_confirm(img)
        if hit:
            return h, hit[0], float(s)
    return None


def leave_room(hwnd=None, dry=False, verbose=True, settle=0.7, tries=3):
    """退出房间：点「退出」→ 弹「是否确认退出房间？」→ 点「确定」。

    返回 'clicked' / 'no_room' / 'no_quit_btn' / 'dry'
    注意：确认框带倒计时自动取消，所以点完「退出」要马上盯框、立刻点「确定」；
    没等到框或点了没反应，会重新点一次「退出」（最多 tries 次）。
    """
    if hwnd is None:
        room = find_room(verbose=verbose, restore=True)
        if not room:
            return "no_room"
        hwnd = room["hwnd"]

    def bye():
        return find_room(verbose=False, restore=False) is None

    for attempt in range(tries):
        if bye():
            if verbose:
                print("   房间窗口已消失（已退出）", flush=True)
            return "clicked"
        img = grab_client(hwnd)
        if img is None:
            return "no_room"
        s_q, pt_q = _score(img, _TPL_RQUIT)
        if pt_q is None or s_q < QUIT_SCORE:
            info = room_info(img)
            if info is None:
                if verbose:
                    print(f"   没找到「退出」按钮（score={s_q:.3f}）、也不是房间画面，放弃退出", flush=True)
                return "no_quit_btn"
            pt_q = (info["anchor"][0] + OFF_QUIT[0], info["anchor"][1] + OFF_QUIT[1])
            if verbose:
                print(f"   模板未命中（score={s_q:.3f}），按锚点偏移点「退出」@{pt_q}", flush=True)
        elif verbose:
            print(f"   「退出」按钮命中 score={s_q:.3f} @{pt_q} → 点它（第 {attempt + 1} 次）", flush=True)
        if dry:
            return "dry"

        _click_quit(hwnd, pt_q)
        time.sleep(min(settle, 0.35))

        # 盯确认框：可能画在房间窗里，也可能是独立 480x300 弹窗，而且只挂 1~4 秒
        for _ in range(6):
            got = find_leave_confirm_any(hwnd)
            if got:
                ch, confirm, sc = got
                if verbose:
                    print(f"   「是否确认退出房间？」命中 score={sc:.3f}"
                          f"（窗口 hwnd={ch}）→ 点「确定」@{confirm}", flush=True)
                c = client_rect(ch)
                try:
                    import popup_guard as _pg
                    _pg.click_screen(ch, c[0] + confirm[0], c[1] + confirm[1])
                except Exception:
                    click_xy(ch, *confirm)
                time.sleep(1.4)
                if bye():
                    if verbose:
                        print("   ✅ 已退出房间", flush=True)
                    return "clicked"
                if verbose:
                    print("   点完「确定」房间还在，重试", flush=True)
                break
            time.sleep(0.4)
        else:
            if bye():
                if verbose:
                    print("   房间窗口已消失（已退出）", flush=True)
                return "clicked"
            if verbose:
                print("   没等到「是否确认退出房间」确认框，重试点「退出」", flush=True)

    # ---------- 兜底：点「退出」没用就来硬的（点窗口右上角 ✕ 那条路） ----------
    # 实测「游戏中」状态的房间：点「退出」完全没反应（按钮看着正常），
    # 但给房间窗发 WM_CLOSE 会弹「是否确认退出房间？」→ 点确定就能退出。
    if bye():
        return "clicked"
    if verbose:
        print("   「退出」点了没反应 → 改用关闭房间窗口（等同点右上角 ✕）", flush=True)
    try:
        import win32con as _wc
        import win32gui as _wg
        _wg.PostMessage(hwnd, _wc.WM_CLOSE, 0, 0)
        t0 = time.time()
        while time.time() - t0 < 6:
            got = find_leave_confirm_any(hwnd)
            if got:
                ch, confirm, sc = got
                if verbose:
                    print(f"   「是否确认退出房间？」命中 score={sc:.3f} → 点「确定」@{confirm}", flush=True)
                c = client_rect(ch)
                try:
                    import popup_guard as _pg
                    _pg.click_screen(ch, c[0] + confirm[0], c[1] + confirm[1])
                except Exception:
                    click_xy(ch, *confirm)
                time.sleep(1.4)
                break
            time.sleep(0.15)
    except Exception as e:
        if verbose:
            print(f"   (关闭房间窗口失败：{e!r})", flush=True)
    if bye():
        if verbose:
            print("   ✅ 已退出房间（走关闭窗口）", flush=True)
        return "clicked"
    return "no_quit_btn"


# ---------------------------------------------------------------- 弹窗
def detect_warn(img):
    """在窗口截图里找「平台提示」弹窗的蓝色「确定」按钮。

    返回 (confirm_center, cancel_center) —— 客户区坐标；没找到返回 None。
    做法：颜色连通域找按钮矩形（蓝色 #0F6BFF 附近），再按固定横向比例推「取消」。
    """
    if img is None:
        return None
    H, W = img.shape[:2]
    b = img[:, :, 0].astype(int)
    g = img[:, :, 1].astype(int)
    r = img[:, :, 2].astype(int)
    mask = ((b > 170) & (r < 115) & (g > 85) & (g < 195)).astype(np.uint8)
    if mask.sum() < 1500:
        return None
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)

    best = None
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 1500 or w < 90:
            continue
        ar = w / max(h, 1)
        if not (2.2 <= ar <= 6.5):
            continue
        cy = y + h / 2.0
        if not (0.15 * H <= cy <= 0.85 * H):     # 弹窗按钮在竖向中部
            continue
        if not (0.02 * W <= x <= 0.9 * W):
            continue
        if not (0.06 * W <= w <= 0.45 * W):      # 弹窗按钮不会占据大半个窗口
            continue
        # 关键判据：「确定」右边紧挨着一个**同高的深灰按钮**（取消）。
        # 平台底部那排「创建房间/快速加入/快速匹配」全是蓝色，右边不是灰的，自然排除。
        px0 = x + w + 3
        px1 = min(W, x + int(w * 1.45))
        if px1 - px0 < 20:
            continue
        reg = img[y:y + h, px0:px1].astype(int)
        if reg.size == 0:
            continue
        rb, rg, rr = reg[:, :, 0], reg[:, :, 1], reg[:, :, 2]
        mean = (rb + rg + rr) / 3.0
        grayish = (np.abs(rb - rg) < 18) & (np.abs(rg - rr) < 18) & (mean > 33) & (mean < 110)
        if grayish.mean() < 0.35:
            continue
        if best is None or area > best[4]:
            best = (x, y, w, h, area)
    if best is None:
        return None
    # 关键区分：「创建房间」对话框也是「蓝色左按钮 + 深灰右按钮」，形状几乎一样，
    # 只靠颜色连通域会把「创建」当成「确定」（→ 误点「取消」直接把建房对话框关掉）。
    # 实测：光比按钮对（确定/取消）区分不开（0.94），必须认「平台提示」标题字样
    # （真弹窗 1.000 / 建房对话框 0.34）。
    if _TPL_WARNLBL is not None:
        lbl_score, _ = _score(img, _TPL_WARNLBL)
        if lbl_score < WARNLBL_SCORE:
            return None
    x, y, w, h, _ = best
    confirm = (int(x + w / 2), int(y + h / 2))
    cancel = (int(x + w / 2 + CANCEL_RATIO * w), int(y + h / 2))
    return confirm, cancel, (x, y, w, h)


def click_xy(h, cx, cy):
    """在客户区坐标点一下（先把窗口抓到前台）"""
    try:
        win32gui.ShowWindow(h, win32con.SW_RESTORE)
    except Exception:
        pass
    # SetForegroundWindow 在别的程序（WorkBuddy）占着前台时会被系统拒绝，点击就落到人家身上。
    # 用 AttachThreadInput 那套强前台，失败再退回老写法。
    try:
        import popup_guard as _pg
        if not _pg.force_foreground(h):
            win32gui.SetForegroundWindow(h)
    except Exception:
        try:
            win32gui.SetForegroundWindow(h)
        except Exception:
            pass
    time.sleep(0.35)
    origin = client_rect(h)
    sx, sy = origin[0] + cx, origin[1] + cy
    win32api.SetCursorPos((sx, sy))
    time.sleep(0.12)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    return sx, sy


def is_on_top(h, tol=None):
    """窗口客户区中心点的最上层窗口是不是它自己。

    2026-11-26 实测：房间窗 (348,72,1224x904) 完全落在主窗 (294,52,1332x945) 内部，
    SetForegroundWindow 之后 GetForegroundWindow 已经是房间窗，但 Z 序没变 ——
    屏幕抓图拿到的还是主窗（详情页）的像素。查不出来时按「已在最前」处理，
    避免乱动窗口（宁可少置顶，不要多置顶）。
    """
    try:
        l, t, r, b = win32gui.GetClientRect(h)
        cx, cy = win32gui.ClientToScreen(h, (max(1, (r - l) // 2), max(1, (b - t) // 2)))
        top = win32gui.WindowFromPoint((cx, cy))
        return win32gui.GetAncestor(top, 2) == h
    except Exception:
        return True


def activate(h, settle=0.5):
    """把窗口恢复并抓到前台（后续点击/截图都要求它在最前面）

    2026-11-26：SetForegroundWindow 只把窗口标成「前台」，**不改 Z 序**。
    所以补一次校验：中心点最上层不是自己就 SetWindowPos(HWND_TOP) 真置顶。
    只在被压住时才动一次，不反复置顶。
    """
    try:
        if win32gui.IsIconic(h):
            win32gui.ShowWindow(h, win32con.SW_RESTORE)
            time.sleep(0.8)
    except Exception:
        pass
    try:
        win32gui.SetForegroundWindow(h)
    except Exception:
        pass
    if not is_on_top(h):
        try:
            win32gui.SetWindowPos(
                h, win32con.HWND_TOP, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        except Exception:
            pass
    time.sleep(settle)
    return h


def handle_popup(prefer_cancel=True, scan_all=False, dry=False, verbose=True):
    """找并点掉弹窗。返回 'clicked' / 'none'"""
    wins = platform_windows()
    if not scan_all:
        # 只看前台那个平台窗口：用户/别的程序在前台时一律不动，
        # 否则会把平台窗口反复置顶，画面乱闪
        fg = win32gui.GetForegroundWindow()
        wins = [w for w in wins if w["hwnd"] == fg]
        if not wins:
            return "none"
    for w in wins:
        if w.get("iconic"):        # 最小化窗口截不到内容，跳过（弹窗只会在前台窗口上）
            continue
        # 建房子弹窗也是蓝色「创建」+ 灰色「取消」，过去曾被误认成此提示。
        # 它是独立的 584x517 窗口；提示显示在平台主窗上。
        cw, ch = w["client"][2:4]
        if 420 <= cw <= 800 and 350 <= ch <= 660:
            continue
        img = grab_client(w["hwnd"])
        hit = detect_warn(img)
        if not hit:
            continue
        confirm, cancel, box = hit
        target = cancel if prefer_cancel else confirm
        name = "取消(留在房间)" if prefer_cancel else "确定(退出房间)"
        if verbose:
            print(f"   弹窗命中 hwnd={w['hwnd']} 按钮框={box} "
                  f"确定@{confirm} 取消@{cancel} → 点 {name} @{target}", flush=True)
        if dry:
            return "found"
        click_xy(w["hwnd"], *target)
        return "clicked"
    return "none"


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--find", action="store_true")
    ap.add_argument("--popup", action="store_true")
    ap.add_argument("--dismiss", action="store_true")
    ap.add_argument("--check", action="store_true", help="校验房间名/密码（名字不是「我在峡谷打」或密码未设 → exit=2）")
    ap.add_argument("--leave", action="store_true", help="点「退出」离开房间")
    ap.add_argument("--confirm", action="store_true", help="点「确定」（会退出房间）")
    ap.add_argument("--all", action="store_true", help="遍历所有平台窗口")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    if args.check:
        hwnd, info = check_room(verbose=True)
        if hwnd is None:
            print("NO_ROOM")
            return 1
        print(f"ROOM_HWND={hwnd} name_ok={info['name_ok']} pwd_ok={info['pwd_ok']} "
              f"name_score={info['name_score']} pwd_white={info['pwd_white']}")
        return 0 if info["name_ok"] and info["pwd_ok"] else 2

    if args.leave:
        r = leave_room(dry=args.dry)
        print("LEAVE:", r)
        return 0 if r in ("clicked", "dry") else 1

    if args.list or not (args.find or args.popup or args.dismiss):
        print(f"平台窗口 {len(platform_windows())} 个：")
        # --list 只做「看一眼」，不还原最小化窗口：游戏在跑的时候还原会把窗口顶到最前面，
        # 把 War3 抢到后台，正在执行的四步就会点空
        room = find_room(restore=False)
        print("→ 房间窗口:", room["hwnd"] if room else "无")
        return 0

    if args.find:
        room = find_room()
        if room:
            print(f"ROOM_HWND={room['hwnd']} client={room['client'][2]}x{room['client'][3]} "
                  f"score={room['score']:.3f}")
            return 0
        print("NO_ROOM")
        return 1

    if args.popup or args.dismiss:
        r = handle_popup(prefer_cancel=not args.confirm, scan_all=args.all, dry=args.dry or args.popup)
        print("POPUP:", r)
        return 0 if r != "none" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
