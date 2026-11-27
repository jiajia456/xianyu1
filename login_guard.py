# -*- coding: utf-8 -*-
"""登录窗口守卫 —— 任务运行中一旦检测到「登录窗口」，立刻停手，不做任何操作。

用户规则（2026-11-17 10:01）
---------------------------
> 如果任务中检测到登录窗口时停止任务不再做任何操作，提示「检测到登录窗口」，时间作为记录显示

也就是说：**只停，不救**。
- 不点任何东西（不点登录、不点关闭、不返回平台、不退游戏、不杀 War3）
- 唯一动作是：把正在跑的自动化子进程树杀掉（否则它还会继续点）
- 时间记进 `logs/login_events.jsonl`，界面/日志醒目提示

判据（三条，任一命中即算）
-------------------------
1. **标题关键词**：可见的 Qt 顶层窗口（类名以 `Qt` 开头）标题含
   登录 / 登陆 / 扫码登录 / 账号登录 / 登录失效 / 重新登录 / 切换账号 / login / sign in
   限定 Qt 类名是为了排除浏览器、记事本、WorkBuddy 等误报。
2. **模板**（可选）：`templates/tpl_login_panel.png` 存在时，在平台主窗截图里匹配
   （score ≥ 0.85）。用于「登录界面嵌在主窗里、标题没变」的情况。
   第一次真遇到登录窗时，跑 `python login_guard.py --grab` 把当时的窗口都存下来，
   手工裁一张给它，之后这条判据就生效。
3. **被顶下线弹窗**（2026-11-17 10:18 用户规则）：
   「平台提示 / 您的账号在另一个地点被登录，因此被迫退出平台」这个弹窗，
   同样视为必须立刻全停的情况。关键词不在标题上（该窗标题就是「KK官方对战平台」），
   所以用模板 `templates/tpl_kick_dialog.png`（弹窗正文那一行字）在中等尺寸的
   Qt 窗口里多尺度匹配，score ≥ 0.80 即命中（实测弹窗 1.000，其余窗口 ≤ 0.49）。
   命中后**不点「确定」**——只是停手并记时间。

用法
----
```bash
python login_guard.py --scan        # 只报当前有没有登录窗口（安全，不动任何东西）
python login_guard.py --grab        # 把当前所有 Qt 窗口截图存到 logs/login_probe/
python login_guard.py --watch       # 前台盯 60 秒，命中就打印
```

被谁用
------
- `engine.Loop`：后台线程每 3s 扫一次 → 命中 → 记 journal + 杀子进程树 + 状态置「检测到登录窗口」
- `full_loop.py`：每轮/每个阶段前扫一次 → 命中 → 直接 return 131（不做事）
- `fishing_mode.py`：静等期间每 60s 扫一次 → 命中 → return 131（不退出游戏）
- `daily_claim.py`：开始前扫一次
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import cv2
import numpy as np
import win32gui

import popup_guard as pg

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")

# 2026-11-27 用户定：**程序自动重启平台后**给登录窗 3 次 × 30 秒宽限
# （平台可能自己登回去；02:25 那轮就是重启后 10 秒弹登录、守卫立刻全停）。
# 宽限期由 plat_restart 写入，所有进程的守卫都读同一个文件。
GRACE_PATH = os.path.join(LOGS, "restart_grace.json")
_GRACE = {"until": 0.0, "ts": 0.0}


def grace_until() -> float:
    """宽限期截止时间（epoch）；0 = 没有宽限。文件 5 秒缓存一次。"""
    now = time.time()
    if now - _GRACE["ts"] < 5.0:
        return _GRACE["until"]
    until = 0.0
    try:
        with open(GRACE_PATH, encoding="utf-8") as f:
            until = float(json.load(f).get("until") or 0)
    except Exception:
        until = 0.0
    _GRACE.update(until=until, ts=now)
    return until

# 标题关键词（小写比对；中文不分大小写但统一处理更省事）
TITLE_TOKENS = (
    "登录", "登陆", "扫码登录", "账号登录", "登录失效", "重新登录",
    "重新登陆", "切换账号", "账号密码", "login", "sign in", "signin",
)

TPL_PANEL = os.path.join(HERE, "templates", "tpl_login_panel.png")
TPL_BTN = os.path.join(HERE, "templates", "tpl_login_btn.png")
TPL_KICK = os.path.join(HERE, "templates", "tpl_kick_dialog.png")
TPL_SCORE = 0.85
TPL_REF_W = 1095     # 模板是从这个宽度的登录窗裁的（2026-11-17 10:03 实测窗口 1095x558）

KICK_SCORE = 0.80    # 「账号在另一个地点被登录」弹窗（实测 1.000，其它窗口 ≤0.49）

MIN_W, MIN_H = 200, 120      # 太小的是气泡/提示，忽略

REASON_LOGIN = "检测到登录窗口"
REASON_KICK = "检测到账号被顶下线（异地登录）"


def _alive(hwnd) -> bool:
    try:
        return bool(win32gui.IsWindow(hwnd)) and bool(win32gui.IsWindowVisible(hwnd))
    except Exception:
        return False


def enum_qt_windows():
    """所有可见的 Qt 顶层窗口（平台/弹窗都是 Qt5152QWindow*）"""
    out = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        try:
            cls = win32gui.GetClassName(h) or ""
        except Exception:
            return
        if not cls.startswith("Qt"):
            return
        try:
            r = win32gui.GetClientRect(h)
        except Exception:
            return
        w, hh = r[2], r[3]
        if w < MIN_W or hh < MIN_H:
            return
        try:
            title = win32gui.GetWindowText(h) or ""
        except Exception:
            title = ""
        out.append(dict(hwnd=h, title=title, cls=cls, w=w, h=hh))

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return out


def _hit_by_title(w):
    low = (w["title"] or "").strip().lower()
    if not low:
        return None
    for tok in TITLE_TOKENS:
        if tok in low:
            return dict(w, source="title", token=tok)
    return None


def _platform_windows():
    """平台相关窗口（标题含「对战平台」；主窗和登录窗都算）"""
    wins = [w for w in enum_qt_windows() if "对战平台" in (w["title"] or "")]
    wins.sort(key=lambda x: x["w"] * x["h"], reverse=True)
    return wins


_TPLCACHE = {}


def _load_tpl(path):
    """模板解码结果缓存（每 3 秒扫一次，别每次都重新解 PNG）"""
    tpl = _TPLCACHE.get(path)
    if tpl is None and os.path.exists(path):
        tpl = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        _TPLCACHE[path] = tpl
    return tpl


def _match_scaled(img, tpl_path, ref_w, score=TPL_SCORE):
    """模板按 img 实际宽度相对 ref_w 缩放后再匹配（窗口大小会变，单一尺度会失配）"""
    if img is None or img.size == 0:
        return None
    tpl = _load_tpl(tpl_path)
    if tpl is None:
        return None
    sc = img.shape[1] / float(ref_w)
    if abs(sc - 1.0) > 0.02:
        tpl = cv2.resize(tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
    if tpl.shape[0] > img.shape[0] or tpl.shape[1] > img.shape[1]:
        return None
    res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    hh, ww = tpl.shape[:2]
    return float(mx), (loc[0] + ww // 2, loc[1] + hh // 2)


def _match_multi(img, tpl_path, lo=0.80, hi=1.25, n=10):
    """多尺度匹配（弹窗尺寸在不同 DPI/分辨率下会变，固定尺度容易失配）"""
    if img is None or img.size == 0:
        return None
    tpl = _load_tpl(tpl_path)
    if tpl is None:
        return None
    best = (0.0, None)
    for sc in np.linspace(lo, hi, n):
        t = cv2.resize(tpl, None, fx=float(sc), fy=float(sc),
                       interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_LINEAR)
        if t.shape[0] > img.shape[0] or t.shape[1] > img.shape[1]:
            continue
        res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx > best[0]:
            hh, ww = t.shape[:2]
            best = (float(mx), (loc[0] + ww // 2, loc[1] + hh // 2))
    if best[1] is None:
        return None
    return best


def _hit_by_kick():
    """「您的账号在另一个地点被登录，因此被迫退出平台」弹窗

    这个弹窗标题就是「KK官方对战平台」（GetWindowText 拿不到「平台提示」），
    标题判据完全测不到，只能看画面。模板是正文那一行字：
    实测弹窗内 1.000，登录窗/主窗/微信窗最高只有 0.49，阈值 0.80 不会误报。
    """
    wins = [w for w in enum_qt_windows() if 240 <= w["w"] <= 1200 and 150 <= w["h"] <= 780]
    wins.sort(key=lambda x: x["w"] * x["h"], reverse=True)
    for w in wins[:4]:
        img = pg.grab_hwnd(w["hwnd"])
        m = _match_multi(img, TPL_KICK, 0.80, 1.25, 10)
        if m and m[0] >= KICK_SCORE:
            return dict(w, source="kick_template", score=round(m[0], 3),
                        at=m[1], kind="kicked")
    return None


def _hit_by_template():
    """兜底：窗口标题没带「登录」二字，但画面就是登录界面（logo「kk! 官方对战平台」）

    不看标题前缀，只看画面 —— 因为掉线后标题可能变成「KK官方对战平台」之类的普通名字。
    logo 模板很独特（在正常地图详情页里最高只有 0.37），阈值 0.85 不会误报。
    只查最大的 3 个窗口，避免每 3 秒把整屏窗口都抓一遍。
    """
    wins = [w for w in enum_qt_windows() if w["w"] >= 700 and w["h"] >= 300]
    wins.sort(key=lambda x: x["w"] * x["h"], reverse=True)
    for w in wins[:3]:
        img = pg.grab_hwnd(w["hwnd"])
        m = _match_scaled(img, TPL_PANEL, TPL_REF_W)
        if m and m[0] >= TPL_SCORE:
            return dict(w, source="template", score=round(m[0], 3), at=m[1])
    return None


# 模板兜底（_hit_by_template）的节流：PrintWindow 抓最大的 3 个 Qt 窗 + 缩放匹配
# ≈ 300ms/次，曾每 3 秒一次 → 常驻吃掉约 10% 单核（2026-11-19 实战 2 局实测，
# ④ 等待段中位 7.8% 全是它贡献的，跟 watch_early 的 60s 轮询无关）。
# 2026-11-19 18:40 用户定：抓图兜底降到 15 秒一次；标题检查（enum+GetWindowText，<2ms）
# 仍每 3 秒一次 —— 登录窗标题一般带「登录」二字，仍 3 秒内发现；
# 只有「标题不变的掉线窗口」这种罕见情形才最多晚 15 秒被模板兜底抓到。
TPL_GAP = 15.0
_TPL_LAST = [0.0]          # 用 list 装时间戳，免 global 声明


# --- 游戏进程门控（2026-11-19 22:46 用户两次定） ---
# 「先检测进程：War3 在 = 对局进行中 → 整轮守卫跳过（标题/抓图都不做）；
#   进程不在（开局前 / 对局结束 / 游戏退出）→ 才做标题检查 + 15 秒抓图兜底」。
# 理由：登录窗/顶号弹窗的高发期是平台暴露期（开局前、对局结束、钓鱼等待后），
# 对局进行中即使弹了也处理不了，等进程消失自然会查到。进程枚举只要几毫秒，
# 比任何一次抓图检查都便宜，放在最前面当总开关。
_WAR3_NAMES = {"war3.exe", "warcraft iii.exe", "warcraft.exe", "war3cn.exe",
               "warcraft iii frozen throne.exe"}   # 与 game_auto.GAME_EXE 一致
_war3_snap = None            # 模块缓存：True/False；None=还没查过
_war3_ts = [0.0]
# 进程枚举结果按「查到什么」分两种缓存时长（2026-11-19 23:50 定，11-20 00:03 收紧）：
#   · 查到「在」（对局中）→ 15 秒后再确认 —— 对局动辄十几分钟，不必勤查
#   · 查到「不在」（平台挂着/开局间隙）→ 10 秒一查 —— 游戏随时可能起来，查勤一点
_WAR3_TTL_ON = 15.0
_WAR3_TTL_OFF = 10.0


def _war3_running() -> bool:
    """War3 游戏进程是否在跑（Toolhelp32 枚举，~几毫秒；结果缓存 5 秒）"""
    global _war3_snap
    now = time.time()
    ttl = _WAR3_TTL_ON if _war3_snap else _WAR3_TTL_OFF
    if _war3_snap is not None and now - _war3_ts[0] < ttl:
        return _war3_snap
    import ctypes
    from ctypes import wintypes as _wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x2

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong),
                    ("cntUsage", ctypes.c_ulong),
                    ("th32ProcessID", ctypes.c_ulong),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", ctypes.c_ulong),
                    ("cntThreads", ctypes.c_ulong),
                    ("th32ParentProcessID", ctypes.c_ulong),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.c_ulong),
                    ("szExeFile", ctypes.c_char * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    found = False
    try:
        entry = PE32()
        entry.dwSize = ctypes.sizeof(PE32)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.decode("mbcs", "ignore").lower()
            if name in _WAR3_NAMES:
                found = True
                break
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    _war3_snap = found
    _war3_ts[0] = now
    return found


_plat_snap = None           # 平台「详情页/房间任一在」缓存：True/False；None=还没查过
_plat_ts = [0.0]
_PLAT_TTL = 30.0            # 平台形态不会秒变，缓存 30 秒


def _platform_normal() -> bool:
    """平台「详情页或房间」任一在 → 平台处于已登录正常态（缓存 30 秒）。

    2026-11-19 22:51 用户定：详情页/房间在也不触发守卫 —— 已登录状态下不会有
    登录窗；平台真正掉到登录态时这些窗口会消失，守卫自然被触发。
    判定用 plat_home.scan（模板级，~100-200ms），缓存 30 秒后均摊可忽略。
    判不了（异常）→ 返回 False，保守地继续走检查。
    """
    global _plat_snap
    now = time.time()
    if _plat_snap is not None and now - _plat_ts[0] < _PLAT_TTL:
        return _plat_snap
    result = False
    try:
        import plat_home as ph
        rows = ph.scan()
        result = any(r.get("face") in ("detail", "room") for r in rows)
    except Exception:
        result = False
    _plat_snap = result
    _plat_ts[0] = now
    return result


def scan(template: bool = True, ignore_grace: bool = False):
    """返回命中列表（空 = 没有登录窗口 / 没有被顶下线）

    ignore_grace=True：忽略「自动重启平台后的宽限期」，给 plat_restart 自己复查用。

    「被顶下线」优先级最高：它一出现，账号已经失效，后面做什么都没意义。

    总开关（2026-11-19 22:46 用户两次定）：**先检测 War3 进程 —— 在就整轮跳过**。
    进程在 = 对局进行中，标题检查和 15 秒抓图兜底都不做；
    进程不在（开局前 / 对局结束 / 游戏退出）才做后面的检查 —— 这正是登录窗高发期。
    进程枚举几毫秒 + 5 秒缓存，本身成本可忽略。

    template=False（2026-11-19 21:43 用户定「待机状态要干净」）：只做标题快查
    （<2ms），跳过两个模板兜底 —— 给 GUI 空闲监视用，纯待机不再有任何抓图/匹配；
    空闲时漏掉的「标题不变的登录窗 / 顶号弹窗」由「点开始前的启动前检查」和
    任务期间的守卫线程（3 秒轮询）兜住，不影响任何动作决策。

    快路径（每次调用都做，<2ms）：_hit_by_kick 的窗口**标题**其实查不到（它标题就是
    「KK官方对战平台」）——不对，kick 弹窗标题查不到，但登录窗标题查得到。
    所以快路径只有标题检查；两个模板兜底（被顶下线 kick × 10 档缩放匹配 × 最多 4 窗、
    登录窗 logo 匹配 × 最多 3 窗）每轮要做几十次 PrintWindow + matchTemplate，
    合计约 0.5~1 秒 —— **统一纳入 TPL_GAP=15 秒节流**。
    2026-11-19 21:26 实测踩坑：GUI 空闲监视每 5 秒调一次 check，kick 不在节流里，
    Qt 抓图又比独立进程慢好几倍 → 程序纯待机被顶到 13.3% 单核（用户任务管理器看到 9%）。
    节流后：运行中守卫每 15 秒做一次完整模板检查，登录窗/顶号弹窗最晚 15 秒发现
    （标题带「登录」的普通登录窗仍每次调用都查、3 秒内发现）。
    """
    if not ignore_grace and time.time() < grace_until():
        return []                      # 自动重启平台的宽限期内 → 不判登录（由 plat_restart 复查）
    if _war3_running():
        return []                      # 游戏进程在 = 对局中 → 本轮全部跳过（总开关）
    if _platform_normal():
        return []                      # 详情页/房间在 = 平台已登录正常态 → 跳过（30 秒缓存）
    hits = []
    for w in enum_qt_windows():
        h = _hit_by_title(w)
        if h:
            hits.append(h)
    if hits or not template:
        return hits
    if time.time() - _TPL_LAST[0] < TPL_GAP:
        return []                      # 节流期内：只做过标题检查，模板兜底还没到期
    _TPL_LAST[0] = time.time()         # 不管命中与否，做完这一轮就重新计时
    kick = _hit_by_kick()
    if kick:
        return [kick]
    h = _hit_by_template()
    if h:
        hits.append(h)
    return hits


def record(hits, extra=None, where=None) -> dict:
    """把命中写进 logs/login_events.jsonl（时间以这里为准）

    `where` 记「谁发现的」：启动前 / GUI空闲 / 第 2 轮 · 段边界 / 钓鱼等待期间…
    """
    os.makedirs(LOGS, exist_ok=True)
    kicked = any(h.get("kind") == "kicked" for h in hits)
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": int(time.time()),
        "kind": "kicked_offline" if kicked else "login_window",
        "reason": REASON_KICK if kicked else REASON_LOGIN,
        "where": where or "",
        "count": len(hits),
        "windows": [{k: h.get(k) for k in ("hwnd", "title", "cls", "w", "h",
                                           "source", "token", "score")}
                    for h in hits],
    }
    if extra:
        rec.update(extra)
    _LAST_HIT.update(ts=rec["ts"], reason=rec["reason"], kind=rec["kind"])
    try:
        with open(os.path.join(LOGS, "login_events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def describe(h) -> str:
    if h.get("source") == "kick_template":
        src = "「账号在另一个地点被登录」弹窗模板 %.3f" % (h.get("score") or 0)
    elif h.get("source") == "title":
        src = "标题命中「%s」" % h.get("token")
    else:
        src = "模板命中 %.3f" % (h.get("score") or 0)
    return (f"「{h.get('title') or '(无标题)'}」{h.get('w')}x{h.get('h')}"
            f" cls={h.get('cls')} hwnd={h.get('hwnd')}（{src}）")


def reason_of(rec) -> str:
    """命中原因文案（老记录没有 reason 字段时退回「检测到登录窗口」）"""
    if not rec:
        return REASON_LOGIN
    return rec.get("reason") or (REASON_KICK if rec.get("kind") == "kicked_offline"
                                 else REASON_LOGIN)


# 最近一次命中的信息（各脚本的 login_abort 打印用它，省得都去存一份）
_LAST_HIT = {"ts": None, "reason": None, "kind": None}


def last_reason() -> str:
    return _LAST_HIT.get("reason") or REASON_LOGIN


def last() -> dict:
    return dict(_LAST_HIT)


_LAST = {"sig": None, "t": 0.0, "rec": None}
_LOCK = threading.Lock()


def check(label: str = "", quiet: bool = False, extra=None, dedup: float = 3.0,
          template: bool = True):
    """检查一次。命中 → 打印醒目提示 + 记录 + 返回 rec；未命中返回 None。

    注意：本函数**不做任何点击/关闭动作**，纯检测。
    `dedup` 秒内扫到同一个窗口（后台线程 + 段边界各扫一次）只打印/记录一次。
    空闲监视那种 5 秒一轮的调用方要传大一点（如 3600），否则日志会被同一条刷屏。
    """
    hits = scan(template=template)
    if not hits:
        # 窗口没了 → 清掉去重记忆，下次再出现必须重新记一行（否则 dedup 大的调用方会漏记）
        with _LOCK:
            if _LAST["rec"] is not None:
                _LAST.update(sig=None, t=0.0, rec=None)
        return None
    sig = tuple(sorted((h.get("hwnd"), h.get("title") or "") for h in hits))
    now = time.time()
    dup = False
    with _LOCK:
        if _LAST["sig"] == sig and now - _LAST["t"] < dedup and _LAST["rec"]:
            rec = _LAST["rec"]          # 刚刚报过了，不重复刷屏
            dup = True
        else:
            rec = record(hits, extra, where=label)
            _LAST.update(sig=sig, t=now, rec=rec)
    if dup:
        return rec
    if not quiet:
        tag = f"（{label}）" if label else ""
        kicked = rec.get("kind") == "kicked_offline"
        print("", flush=True)
        print("=" * 64, flush=True)
        print(f"⛔ {reason_of(rec)}{tag}  时间 {rec['ts']}", flush=True)
        for h in hits:
            print(f"   → {describe(h)}", flush=True)
        if kicked:
            print("   账号已在别处登录，本机登录态已失效 → 立刻全停。", flush=True)
            print("   已按规则停止任务：不点「确定」、不重新登录、不关窗口、不退游戏。", flush=True)
        else:
            print("   已按规则停止任务：不点登录、不关窗口、不返回平台、不退游戏。", flush=True)
        print("   记录：logs/login_events.jsonl", flush=True)
        print("=" * 64, flush=True)
    return rec


def watch_loop(stop_event, on_hit, interval: float = 3.0, label: str = "运行中"):
    """后台监视线程体：命中后回调 on_hit(rec) 并退出（只回调一次）"""
    while not stop_event.is_set():
        rec = check(label, quiet=False)
        if rec:
            try:
                on_hit(rec)
            except Exception:
                pass
            return rec
        # 拆成小步睡，stop 能立刻响应
        spent = 0.0
        while spent < interval and not stop_event.is_set():
            time.sleep(0.2)
            spent += 0.2
    return None


# ---------------------------------------------------------------- 自检 / 探测
def grab_probe(tag: str = "") -> str:
    """把所有 Qt 窗口截图存下来（真遇到登录窗时用来裁模板）"""
    d = os.path.join(LOGS, "login_probe")
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    n = 0
    for w in enum_qt_windows():
        img = pg.grab_hwnd(w["hwnd"])
        if img is None:
            continue
        name = f"{stamp}_{w['hwnd']}_{w['w']}x{w['h']}{('_' + tag) if tag else ''}.png"
        try:
            cv2.imencode(".png", img)[1].tofile(os.path.join(d, name))
            n += 1
            print(f"   {name}  标题={w['title']!r}", flush=True)
        except Exception as e:
            print(f"   存图失败 {w['hwnd']}: {e!r}", flush=True)
    print(f"✅ 已存 {n} 张到 {d}", flush=True)
    return d


def main():
    pg.enable_dpi_awareness()
    args = sys.argv[1:]
    if "--grab" in args:
        grab_probe("manual")
        return 0
    if "--watch" in args:
        secs = 60
        for i, a in enumerate(args):
            if a == "--seconds" and i + 1 < len(args):
                secs = int(args[i + 1])
        t0 = time.time()
        print(f"👀 盯着登录窗口 {secs}s（Ctrl-C 退出）…", flush=True)
        while time.time() - t0 < secs:
            if check("监视"):
                return 2
            time.sleep(3)
        print("✅ 这段时间没检测到登录窗口", flush=True)
        return 0

    # 默认/--scan：只报
    wins = enum_qt_windows()
    print(f"🪟 可见 Qt 窗口 {len(wins)} 个：", flush=True)
    for w in wins:
        print(f"   {w['hwnd']:>9} {w['w']}x{w['h']} {w['title']!r} {w['cls']}", flush=True)
    print(f"🧩 模板 tpl_login_panel.png {'存在' if os.path.exists(TPL_PANEL) else '未建立（靠标题判据）'}",
          flush=True)
    print(f"🧩 模板 tpl_kick_dialog.png {'存在（顶号弹窗可测）' if os.path.exists(TPL_KICK) else '未建立'}",
          flush=True)
    rec = check("扫描")
    if rec:
        return 2
    print("✅ 没检测到登录窗口", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
