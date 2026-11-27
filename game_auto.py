# -*- coding: utf-8 -*-
"""
KK官方对战平台 通用游戏自动化引擎
=====================================
目标程序：E:\\kkduizhan\\Platform.exe （平台本体）
游戏模块：Games\\emulator\\KKEmulator.exe（街机/FCEUmm）、Games\\y3（魔兽RPG）

设计要点：
  * 输入走 DirectInput 扫描码（pydirectinput），模拟真实按键，可被游戏接受
  * 图像识别走 cv2.matchTemplate，坐标一律使用「客户区相对坐标」
  * 任务用 JSON 描述，支持 循环 / 条件判断(图像·像素) / 等待 / 点击 / 连发
  * 热键：F8 开始-暂停  F9 停止  F10 紧急释放（松开所有按住的键）
  * 所有延时/点击位置带随机抖动，避免机械式定值操作

用法：
  python game_auto.py --list-windows
  python game_auto.py --config configs\\arcade.json --task main
  python game_auto.py --config configs\\war3.json --task farm --autostart
  python game_auto.py --shot shot.png --window-title KKEmulator
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import re
import sys
import time
import threading
import subprocess
from ctypes import wintypes
from datetime import datetime

import numpy as np
import cv2
import mss
import keyboard
import pydirectinput
import win32gui
import win32ui
import win32con

# 调试探针（2026-11-25）：勾了界面「调试」才有开销，关着时只有一次字符串比较。
# 模块缺失/出错绝不能拖累主流程 —— 全部调用点都判 None。
try:
    import dbgprobe as _DBG
except Exception:                       # pragma: no cover
    _DBG = None

# ----------------------------------------------------------------------------
# Win32 基础
# ----------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# pywin32 的 win32gui 没有导出 PrintWindow，直接用 ctypes 声明
_print_window = user32.PrintWindow
_print_window.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_print_window.restype = wintypes.BOOL

# ---------------------------------------------------------------------------
# SendInput（用于输入中文等 Unicode 文本，pydirectinput 只能打 ASCII 键名）
# ---------------------------------------------------------------------------
ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
_send_input = user32.SendInput
_send_input.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_send_input.restype = wintypes.UINT


def send_unicode_char(ch: str) -> int:
    """把一个 UTF-16 码元以 WM_CHAR 方式敲进去（支持中文）"""
    units = ch.encode("utf-16-le")
    sent = 0
    for i in range(0, len(units), 2):
        code = units[i] | (units[i + 1] << 8)
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = _INPUT(type=INPUT_KEYBOARD)
            inp.u.ki = _KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags,
                                   time=0, dwExtraInfo=None)
            sent += _send_input(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    return sent
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def is_elevated(pid: int):
    """该进程是否以管理员身份运行。查不到返回 None（权限不足/进程已退出）。

    用处：游戏以管理员跑、我们不是的时候，Windows 的 UIPI 会拦掉切前台和点击 ——
    现象是「日志一切正常、游戏纹丝不动」，从日志上完全看不出是权限问题。
    """
    if not pid:
        return None
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        tok = wintypes.HANDLE()
        if not ctypes.windll.advapi32.OpenProcessToken(h, 0x0008,  # TOKEN_QUERY
                                                       ctypes.byref(tok)):
            return None
        try:
            val = ctypes.c_int(0)
            n = wintypes.DWORD(0)
            if not ctypes.windll.advapi32.GetTokenInformation(
                    tok, 20,  # TokenElevation
                    ctypes.byref(val), ctypes.sizeof(val), ctypes.byref(n)):
                return None
            return bool(val.value)
        finally:
            kernel32.CloseHandle(tok)
    finally:
        kernel32.CloseHandle(h)


_EXE_CACHE = {}          # pid -> (exe 名, 时间戳)
_EXE_TTL = 5.0           # 秒；等窗口的循环里 1 秒扫一次，缓存能省掉绝大部分 OpenProcess


def pid_to_exe(pid: int) -> str:
    """pid → 进程 exe 名（5 秒缓存：同一个 pid 反复查不再开句柄）。

    enum_windows_full 每次都会把所有可见窗口的 pid 查一遍，等窗口/等难度的循环里
    1 秒调一次。2026-11-18 16:30 实测（本机 11 个可见窗口）：0.2ms → 0.1ms，
    收益很小，保留是因为窗口特别多（40+，开着多个 IDE/浏览器/游戏）时每个窗口都要
    OpenProcess，那个场景才值得省。真正的收益在下面的 score() 打分，不在缓存。
    """
    if not pid:
        return ""
    hit = _EXE_CACHE.get(pid)
    if hit and (time.time() - hit[1]) < _EXE_TTL:
        return hit[0]
    name = ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(h)
    try:
        _EXE_CACHE[pid] = (name, time.time())
        if len(_EXE_CACHE) > 500:      # pid 会复用，别无限增长
            _EXE_CACHE.clear()
    except Exception:
        pass
    return name


def enum_windows_full():
    """所有可见顶层窗口的详细信息（dict 列表）。

    字段：hwnd / title / exe / w / h / offscreen / iconic
    offscreen=True 表示「最小化到屏幕外」（GetWindowRect ≈ -32000）—— 客户区尺寸仍是还原后
    的大小。2026-11-17 19:11 用户那台电脑的现场就是这个：点了开始游戏，War3 已经起来了，
    但窗口被压到任务栏/屏外，旧代码直接把这类窗口丢弃 → 主程序一直「等窗口出现」。
    """
    result = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        wr = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        # 最小化到屏幕外的窗口不再直接丢弃，交给调用方决定（优先用正常窗口，没有再用它）
        off = wr.left <= -10000 or wr.top <= -10000
        n = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rc = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        cbuf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cbuf, 256)
        result.append({
            "hwnd": hwnd,
            "title": title,
            "cls": cbuf.value,
            "exe": pid_to_exe(pid.value),
            "w": rc.right - rc.left,
            "h": rc.bottom - rc.top,
            "pid": pid.value,
            "offscreen": off,
            "iconic": bool(user32.IsIconic(hwnd)),
        })
        return True

    user32.EnumWindows(_cb, 0)
    return result


# 「这个窗口可能是游戏窗口吗」的通用判定。
# 2026-11-18 18:43 用户提的思路：别死盯句柄，先看**画面内容**，谁画出大厅就绑谁。
# 要按画面找，第一步就得把「平台的窗口/本程序界面/资源管理器」先排除干净，
# 否则平台详情页里也有「开始游戏」四个字，一匹配就绑错。
BAD_CLS_PREFIX = ("Qt",)      # 平台、本程序都是 Qt 窗口
BAD_CLS = {"CabinetWClass", "Progman", "WorkerW", "Shell_TrayWnd",
           "TaskListThumbnailWnd"}
BAD_EXE = {"platform.exe", "explorer.exe", "python.exe", "pythonw.exe", "notepad.exe"}
# 只认这几支 exe 是「游戏本体」（2026-11-19 血泪）：
# 原来用黑名单排除平台/本程序/资源管理器，剩下的大窗口全算候选 —— 结果 War3 刚启动、
# 窗口还是黑的时，「按画面找窗口」扫到了 WorkBuddy 窗口（1438x900）并在上面匹配到
# 开始按钮 score=1.000，把 self.win 切过去，整轮难度点击/开始游戏全打在错窗口上，
# 局内四步 0/3、0/4 全废。改成白名单：exe 拿得到就只认 War3。
GAME_EXE = {"war3.exe", "warcraft iii.exe", "warcraft.exe", "war3cn.exe",
            "warcraft iii frozen throne.exe"}
BAD_TITLE = ("对战平台", "三大流程循环", "平台提示", "创建房间")


def is_game_candidate(w: dict, min_size: int = 400) -> bool:
    """排除平台窗 / 本程序界面 / 资源管理器 / 最小化的窗口，剩下的才可能是 War3"""
    t = w.get("title") or ""
    if w["w"] < min_size or w["h"] < min_size:
        return False
    if w.get("offscreen") or w.get("iconic"):
        return False
    exe = (w.get("exe") or "").lower()
    if exe:
        # exe 拿得到 → 白名单：只认 War3 本体，其它一律不当游戏窗口。
        # （2026-11-19：WorkBuddy 窗口曾被当成 War3 绑过去，见 GAME_EXE 注释）
        if exe in GAME_EXE:
            return True
        # 兜底：个别汉化/魔改版改过 exe 名，但窗口标题一定带 Warcraft / 魔兽。
        # 比原来的黑名单严格得多 —— WorkBuddy / chrome / QQ 都不会命中。
        return ("warcraft" in t.lower()) or ("魔兽" in t)
    # exe 拿不到（个别权限受限环境）→ 退回原来的黑名单判断
    if any(k in t for k in BAD_TITLE):
        return False
    cls = w.get("cls") or ""
    if cls in BAD_CLS or cls.startswith(BAD_CLS_PREFIX):
        return False
    return True


class _WinShim:
    """给 Capture.grab 用的假窗口（只需要 hwnd + client_rect）"""

    def __init__(self, hwnd: int, rect):
        self.hwnd = hwnd
        self._rect = rect

    def client_rect(self):
        return self._rect


def window_report(maxn: int = 8) -> str:
    """一行摘要：当前有哪些可见窗口。用来回答「游戏明明启动了，为什么绑不上」。"""
    ws = enum_windows_full()
    ws.sort(key=lambda w: w["w"] * w["h"], reverse=True)
    if not ws:
        return "当前没有任何可见窗口（游戏是不是还在启动？）"
    parts = [f"{w['exe'] or '?'} {w['w']}x{w['h']} {w['title'][:24]!r}"
             f"{'[最小化]' if w['offscreen'] else ''}" for w in ws[:maxn]]
    more = f" …共 {len(ws)} 个" if len(ws) > maxn else ""
    return f"当前可见窗口 {len(ws)} 个： " + " ｜ ".join(parts) + more


def enum_windows():
    """返回所有可见顶层窗口 [(hwnd, title, exe, w, h)]（跳过最小化到屏幕外的）"""
    return [(w["hwnd"], w["title"], w["exe"], w["w"], w["h"])
            for w in enum_windows_full() if not w["offscreen"]]


# ----------------------------------------------------------------------------
# 窗口
# ----------------------------------------------------------------------------
class GameWindow:
    def __init__(self, title_re=None, exe=None, min_size: int = 200,
                 pick: str = "largest", size_hint=None):
        if isinstance(title_re, str):
            title_re = [title_re]
        self.title_res = [re.compile(t, re.I) for t in (title_re or [])]
        if isinstance(exe, str):
            exe = [exe]
        self.exe_names = [e.lower() for e in (exe or [])]
        self.min_size = min_size
        self.pick = (pick or "largest").lower()   # largest / smallest
        # 目标尺寸（如房间窗 1224x904）：给了就按「尺寸最接近」挑。
        # 比 pick=smallest 稳：平台会弹出 584x517 的提示小窗，smallest 会绑到它上面。
        self.size_hint = size_hint
        self.pid = 0
        self.hwnd = self._find()

    def _find(self) -> int:
        wins = enum_windows_full()
        have_rule = bool(self.exe_names or self.title_res)

        def score(w) -> int:
            """匹配打分：exe 精确 3 > exe 前缀 2 > 标题 1；0 = 不匹配。

            以前是「exe 命中 或 标题命中」平权 OR，两条都能进候选、最后按面积挑。
            换电脑时标题可能被地图名/皮肤改掉，也可能别的窗口标题里恰好带 Warcraft，
            平权就会绑错窗口（例：war3.json 里同时写了 Platform.exe，War3 没起来时
            平台窗面积最大 → 绑到平台上对着发按键）。
            改成分数后：只要进程名对得上，就永远优先于「只有标题像」的窗口。
            """
            e = (w["exe"] or "").lower()
            e_stem = e[:-4] if e.endswith(".exe") else e
            best = 0
            for name in self.exe_names:
                n_stem = name[:-4] if name.endswith(".exe") else name
                # 完全同名，或「war3.exe」对上「war3 1.27.exe」这种带版本号的进程名
                if e and (e == name or e_stem == n_stem):
                    best = 3
                    break
                if e_stem and e_stem.startswith(n_stem):
                    best = max(best, 2)
            if best == 0 and self.title_res and any(
                    r.search(w["title"] or "") for r in self.title_res):
                best = 1
            return best

        cands, off_cands, small, others = [], [], [], []
        for w in wins:
            sc = score(w) if have_rule else 1
            if have_rule and not sc:
                others.append(w)
                continue
            if w["w"] < self.min_size or w["h"] < self.min_size:
                small.append(w)
                continue
            w["_score"] = sc
            (off_cands if w["offscreen"] else cands).append(w)

        if not cands and off_cands:
            log.warning("找到游戏窗口了，但它处于最小化（屏幕外）状态 → 还原后继续")
            cands = off_cands

        if not cands:
            msg = (f"找不到窗口：exe={self.exe_names} 或 title~="
                   f"{[r.pattern for r in self.title_res]}（要求最小 {self.min_size}px）")
            if small:
                msg += "；有同名窗口但尺寸太小被过滤：" + "; ".join(
                    f"{w['exe']} {w['w']}x{w['h']} {w['title'][:20]!r}" for w in small[:4])
                msg += "；→ 把配置里的 min_size 调小"
            elif others:
                msg += "；" + window_report(6)
                msg += ("；→ 游戏窗口的进程名/标题和配置对不上。把这行日志发我即可，"
                        "或在 core 目录跑 `KKAuto.exe game_auto.py list-windows`"
                        "（结果会写成 logs/list_windows.txt）")
            else:
                msg += "；" + window_report(6)
            raise RuntimeError(msg)

        hint = getattr(self, "size_hint", None)
        if hint:
            # 指定了目标尺寸（例如房间窗固定 1224x904）→ 按「尺寸最接近」挑，
            # 避免 pick=smallest 遇上平台弹出的 584x517 小提示窗时绑错窗口。
            hw_, hh_ = float(hint[0]), float(hint[1])
            cands.sort(key=lambda w: (
                -w.get("_score", 0),
                (abs(w["w"] - hw_) + abs(w["h"] - hh_)) / (hw_ + hh_)))
        else:
            # 先按匹配分（exe 精确 > 前缀 > 标题），同分再按面积 + pick 方向
            cands.sort(key=lambda w: (
                -w.get("_score", 0),
                -w["w"] * w["h"] if self.pick != "smallest" else w["w"] * w["h"]))
        # ④ 房间步骤：③ 已经按「新窗口」绑定了房间 hwnd → 优先挑它。
        # 详情页和房间窗都是 1224x904，按尺寸/面积猜在 W7 上绑错过一次；
        # 候选里没有这扇窗（被关掉了）就退回原规则，不影响别的步骤。
        _pref = PREFER_HWND.get("hwnd")
        if _pref:
            _pi = next((k for k, w in enumerate(cands) if w["hwnd"] == _pref), None)
            if _pi is not None:
                cands.insert(0, cands.pop(_pi))
                log.info(f"   优先使用已绑定的房间窗 hwnd={_pref}")
            else:
                log.info(f"   （已绑定的房间窗 hwnd={_pref} 不在候选里 → 按原规则挑）")
        picked = cands[0]
        hwnd, title, exe, w, h = (picked["hwnd"], picked["title"], picked["exe"],
                                  picked["w"], picked["h"])
        self.pid = picked["pid"] or 0
        if not self.pid:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            self.pid = pid.value
        if picked["offscreen"] or picked["iconic"]:
            user32.ShowWindow(hwnd, 9)      # SW_RESTORE：最小化的先还原，否则截图/点击全废
            time.sleep(0.4)
        log.info(f"绑定窗口: hwnd={hwnd} pid={self.pid} title={title!r} exe={exe} "
                 f"客户区={w}x{h} pick={self.pick}")
        # 权限一致性自检：游戏提权而我们没有 → 切前台/点击会被 UIPI 静默拦掉
        try:
            g_admin, me_admin = is_elevated(self.pid), is_elevated(os.getpid())
            if g_admin and not me_admin:
                log.warning("⚠ 游戏是【管理员】身份运行的，本程序不是 → 切前台/点击可能被系统"
                            "拒绝（表现为游戏完全没反应）。请关掉本程序，右键「以管理员身份运行」")
            elif g_admin is not None:
                log.info(f"权限一致：游戏管理员={g_admin} 本程序管理员={me_admin}")
        except Exception:
            pass
        return hwnd

    def fg_same_process(self) -> bool:
        """前台窗口是否属于同一个进程（弹窗、房间窗口也算，别把焦点抢回主窗）"""
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        if fg == self.hwnd:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        return pid.value == self.pid

    def alive(self) -> bool:
        return bool(user32.IsWindow(self.hwnd))

    def client_rect(self):
        """客户区在屏幕上的 (left, top, width, height)"""
        rc = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rc))
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y, rc.right - rc.left, rc.bottom - rc.top

    def focus(self) -> bool:
        """把窗口切到前台，失败时用 AttachThreadInput 兜底"""
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)  # SW_RESTORE
        try:
            user32.SetForegroundWindow(self.hwnd)
        except Exception:
            pass
        if user32.GetForegroundWindow() == self.hwnd:
            return True
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_me = kernel32.GetCurrentThreadId()
        try:
            user32.AttachThreadInput(tid_me, tid_fg, True)
            user32.BringWindowToTop(self.hwnd)
            user32.SetForegroundWindow(self.hwnd)
            user32.AttachThreadInput(tid_me, tid_fg, False)
        except Exception:
            pass
        time.sleep(0.15)
        return user32.GetForegroundWindow() == self.hwnd

    def is_foreground(self) -> bool:
        return user32.GetForegroundWindow() == self.hwnd

    def ensure_foreground(self, tries: int = 3, wait: float = 0.3) -> bool:
        """确保本窗口在前台，返回是否成功。

        点击前必须做这一步：pydirectinput 发的是**全局**鼠标事件，窗口不在前台时
        点击会打在别人身上，表现为「点了完全没反应」，而且截图里看不出异常
        （PrintWindow/ImageGrab 照样能抓到窗口内容）。
        实测 2026-11-16：海岛/不死弹窗的「否」按钮，War3 在前台时第 2 次必中；
        不在前台时连点 5 次弹窗纹丝不动，最后靠弹窗自身超时才消失。
        """
        for _ in range(max(1, tries)):
            if user32.GetForegroundWindow() == self.hwnd:
                return True
            self.focus()
            # Win7 老机器切前台更慢，按档位放宽（configs/os_profile.json 的 focus_wait_scale）
            try:
                import sysenv as _se
                time.sleep(wait * float(_se.focus_wait_scale()))
            except Exception:
                time.sleep(wait)
        return user32.GetForegroundWindow() == self.hwnd


# ----------------------------------------------------------------------------
# 截屏
# ----------------------------------------------------------------------------
class Capture:
    """mode: auto / printwindow / screen

    auto  = 优先 PrintWindow 后台抓取（不置顶、不抢前台）；抓不到有效画面
            （纯黑/纯色，典型是 Win7 关 Aero）才退回屏幕抓取。
            2026-11-26 实测反过来更糟：房间窗被主窗整扇盖住时，屏幕抓图拿到的是
            主窗（详情页）的像素，④ 拿详情页去找观战框/开始游戏必然全 miss。
    """

    def __init__(self, mode: str = "auto"):
        # 按系统档位决定抓图方式（configs/os_profile.json 可覆盖）：
        # Win7 关 Aero 时 PrintWindow 常返黑图，配置里可强制 screen
        try:
            import sysenv as _se
            mode = _se.capture_mode(mode)
        except Exception:
            pass
        self.mode = mode
        self._sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()

    def grab(self, win: GameWindow) -> np.ndarray:
        x, y, w, h = win.client_rect()
        if w <= 0 or h <= 0:
            raise RuntimeError("窗口客户区尺寸为 0，可能已最小化")

        if self.mode == "printwindow":
            img = self._printwindow(win, w, h)
            return img if img is not None else self._screen(x, y, w, h)

        if self.mode == "screen":
            return self._screen(x, y, w, h)

        # auto：先用 PrintWindow 读窗口自己的绘制内容 —— 窗口被别的窗口压住也读得到。
        # 2026-11-26 实测：房间窗 (348,72,1224x904) 完全落在主窗 (294,52,1332x945) 内部，
        # activate() 之后 GetForegroundWindow 已是房间窗，但 Z 序没变、屏幕上仍是主窗像素，
        # 屏幕抓图拿到的是详情页 → ④ 找不到观战框/开始游戏。
        # 只有 PrintWindow 拿不到有效画面（std<1.5，纯黑，典型 Win7 关 Aero）才退回屏幕抓图。
        alt = self._printwindow(win, w, h)
        if alt is not None and float(alt.std()) >= 1.5:
            return alt
        return self._screen(x, y, w, h)

    def _screen(self, x, y, w, h) -> np.ndarray:
        raw = self._sct.grab({"left": x, "top": y, "width": w, "height": h})
        return cv2.cvtColor(np.array(raw, dtype=np.uint8), cv2.COLOR_BGRA2BGR)

    def _printwindow(self, win: GameWindow, w: int, h: int):
        hwnd_dc = None
        mfc_dc = save_dc = bmp = None
        try:
            hwnd_dc = win32gui.GetWindowDC(win.hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bmp)
            _print_window(win.hwnd, save_dc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT            info = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            arr = np.frombuffer(bits, dtype=np.uint8)
            arr = arr.reshape((info["bmHeight"], info["bmWidth"], 4))
            return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR).copy()
        except Exception:
            return None
        finally:
            try:
                if bmp is not None:
                    win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
            try:
                if save_dc is not None:
                    save_dc.DeleteDC()
            except Exception:
                pass
            try:
                if mfc_dc is not None:
                    mfc_dc.DeleteDC()
            except Exception:
                pass
            try:
                if hwnd_dc is not None:
                    win32gui.ReleaseDC(win.hwnd, hwnd_dc)
            except Exception:
                pass


# ----------------------------------------------------------------------------
# 图像识别
# ----------------------------------------------------------------------------
class Vision:
    def __init__(self, template_dir: str):
        self.template_dir = template_dir
        self._cache = {}

    def _load(self, name: str):
        path = name if os.path.isabs(name) else os.path.join(self.template_dir, name)
        if not os.path.exists(path):
            raise FileNotFoundError(f"模板不存在: {path}")
        mtime = os.path.getmtime(path)
        key = (path, mtime)
        if key not in self._cache:
            data = np.fromfile(path, dtype=np.uint8)  # 兼容中文路径
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None:
                raise RuntimeError(f"模板读不出来: {path}")
            self._cache[key] = img
        return self._cache[key]

    @staticmethod
    def _flat_patch(img, hit, sx: int, sy: int, min_std: float = 6.0) -> bool:
        """命中区域是不是「几乎纯色」（TM_CCOEFF_NORMED 对均匀区域会给 1.0 假阳性）

        2026-11-19：WorkBuddy 窗口上出现过 score=1.000 的假命中，把游戏窗口绑错。
        exe 白名单已经堵住那个入口，这里再加一道：模板有内容、画面那块却是纯色时，
        这个 1.000 不可信。模板本身低对比（std<8）的按钮不受影响。
        """
        try:
            x, y = int(hit[0] - sx), int(hit[1] - sy)
            w, h = int(hit[2]), int(hit[3])
            if x < 0 or y < 0 or x + w > img.shape[1] or y + h > img.shape[0]:
                return False
            return float(img[y:y + h, x:x + w].std()) < min_std
        except Exception:
            return False

    @staticmethod
    def _refine(img, tpl0, coarse, sx: int, sy: int, pad: int = 8):
        """在 0.5x 粗筛的候选点附近，用全尺寸模板精确认一次（几毫秒）"""
        _mv, _ml, _fx, _fy = coarse
        tw = max(1, int(round(tpl0.shape[1] * _fx)))
        th = max(1, int(round(tpl0.shape[0] * _fy)))
        tpl = tpl0 if (abs(_fx - 1) < 1e-6 and abs(_fy - 1) < 1e-6) else \
            cv2.resize(tpl0, (tw, th), interpolation=cv2.INTER_AREA)
        cx, cy = int(_ml[0]) * 2, int(_ml[1]) * 2      # 半尺寸坐标 → 全尺寸
        x0, y0 = max(0, cx - pad), max(0, cy - pad)
        x1 = min(img.shape[1], cx + tpl.shape[1] + pad)
        y1 = min(img.shape[0], cy + tpl.shape[0] + pad)
        sub = img[y0:y1, x0:x1]
        if sub.shape[0] < tpl.shape[0] or sub.shape[1] < tpl.shape[1]:
            return None
        res = cv2.matchTemplate(sub, tpl, cv2.TM_CCOEFF_NORMED)
        _, mv2, _, ml2 = cv2.minMaxLoc(res)
        return (sx + x0 + int(ml2[0]), sy + y0 + int(ml2[1]),
                tpl.shape[1], tpl.shape[0], float(mv2))

    def find(self, name: str, screen: np.ndarray, threshold: float = 0.85,
             region=None, scales=None):
        """找图对外入口：真正干活的是 _find，这里只挂调试记录（2026-11-25）

        为什么要包一层：图色异常时最需要的就是「这张模板在这台机器上到底多少分」，
        而原实现没命中就 return None，分数直接丢了，只能靠猜阈值。
        """
        hit = self._find(name, screen, threshold, region, scales)
        if _DBG is not None and _DBG.on():
            try:
                _DBG.on_find(name, threshold, hit, screen, self, region, scales)
            except Exception:
                pass
        return hit

    def _find(self, name: str, screen: np.ndarray, threshold: float = 0.85,
              region=None, scales=None):
        """返回 (x, y, w, h, score) 客户区坐标，找不到返回 None

        scales: 需要多尺度匹配时传列表，例如 [0.8,0.9,...,1.6]
                （用户从缩小视图里截的图，和实际渲染尺寸不一致时用）
        """
        tpl0 = self._load(name)
        sx, sy = 0, 0
        img = screen
        if region:
            rx, ry, rw, rh = region
            img = screen[ry:ry + rh, rx:rx + rw]
            sx, sy = rx, ry

        slist = list(scales or [1.0])

        # ── 0.5x 粗筛（2026-11-19 降 CPU）──────────────────────────────────
        # 为什么：一次全窗口 matchTemplate 在 1690x921 上约 96ms，配 12 档就是
        # 1.08 秒/帧，而 interval 只有 1000ms —— 等大厅按钮那 15 分钟等于把
        # 一个核算满了烧。半尺寸匹配只要 1/4 成本（约 24ms），且实测分数几乎不降：
        #   有按钮 + 正确缩放档 → 0.998~1.000
        #   没按钮（纯背景 / 别的界面当背景）→ 最高 0.405
        # 所以先用最可能的 2 档（等比 1.0 + 按当前窗口算出的 fx/fy，后者才是答案）
        # 粗筛一次：没希望就直接返回，省掉后面所有档；很有把握就就地精确认。
        # 只在画面和模板都够大时启用（小模板半尺寸下会糊）。
        if img.shape[0] >= 200 and img.shape[1] >= 200 and min(tpl0.shape[:2]) >= 20:
            try:
                half = cv2.resize(img, None, fx=0.5, fy=0.5,
                                  interpolation=cv2.INTER_AREA)
                # 探针档：一定要包含「1.0」和「第一个 (fx,fy)」——
                # 后者才是按当前窗口算出的真答案，绝不能被死取前 2 档漏掉。
                _probe = []
                for _s in slist:
                    if isinstance(_s, (tuple, list)):
                        _probe.append(_s)
                        break
                if not any(isinstance(_x, (int, float)) and abs(_x - 1.0) < 1e-6
                           for _x in _probe):
                    for _s in slist:
                        if isinstance(_s, (int, float)) and abs(_s - 1.0) < 1e-6:
                            _probe.insert(0, _s)
                            break
                if not _probe:
                    _probe = slist[:2]
                coarse = None
                for _s in _probe:
                    _fx, _fy = ((_s, _s) if isinstance(_s, (int, float))
                                else (float(_s[0]), float(_s[1])))
                    _tw = max(8, int(round(tpl0.shape[1] * _fx * 0.5)))
                    _th = max(8, int(round(tpl0.shape[0] * _fy * 0.5)))
                    if half.shape[0] < _th or half.shape[1] < _tw:
                        continue
                    _ts = cv2.resize(tpl0, (_tw, _th),
                                     interpolation=cv2.INTER_AREA)
                    _res = cv2.matchTemplate(half, _ts, cv2.TM_CCOEFF_NORMED)
                    _, _mv, _, _ml = cv2.minMaxLoc(_res)
                    if coarse is None or _mv > coarse[0]:
                        coarse = (float(_mv), _ml, _fx, _fy)
                if coarse is not None:
                    if coarse[0] < 0.60:
                        return None          # 画面里没这东西 → 省下全部档位
                    if coarse[0] >= threshold - 0.05:
                        # 粗筛已经很有把握 → 只在候选点附近做全尺寸精确认（几毫秒）
                        _ref = self._refine(img, tpl0, coarse, sx, sy)
                        if _ref is not None and _ref[4] >= threshold \
                                and not (tpl0.std() >= 8
                                         and self._flat_patch(img, _ref, sx, sy)):
                            return _ref
            except Exception:
                pass                         # 粗筛出岔子就老实走全尺寸

        best = None
        for s in slist:
            # s 允许是单个数（等比），也允许 (fx, fy) 二元组。
            # 换电脑后游戏窗口常常是「横向被压扁」的：那台机器上 War3 客户区
            # 1264x921 / 1200x830，而模板全是在 1690x921 下截的 —— fx=0.748、
            # fy=1.0，只试等比缩放（fx=fy）永远匹配不上。见 Bot._scales_with_ref()。
            fx, fy = (s, s) if isinstance(s, (int, float)) else (float(s[0]), float(s[1]))
            tpl = (tpl0 if (abs(fx - 1) < 1e-6 and abs(fy - 1) < 1e-6)
                   else cv2.resize(tpl0, None, fx=fx, fy=fy, interpolation=cv2.INTER_AREA))
            th, tw = tpl.shape[:2]
            if img.shape[0] < th or img.shape[1] < tw:
                continue
            res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if best is None or max_val > best[4]:
                best = (sx + max_loc[0], sy + max_loc[1], tw, th, float(max_val))
            # 短路（2026-11-19 降 CPU）：已经明显命中就不用再试其它缩放档。
            # 实测 1690x921 全窗口一次 matchTemplate 约 96ms，3 档连跑约 300ms，
            # 而正常命中都在 0.95 以上 —— 留 0.04 余量，避免「刚过线就短路」
            # 把分数更高的那一档（更准的坐标）挡掉。
            if best[4] >= threshold + 0.04:
                break
        if best is None or best[4] < threshold:
            return None
        # 纯色块假阳性：模板有内容、命中区域却是纯色 → 这个分数不可信
        if tpl0.std() >= 8 and self._flat_patch(img, best, sx, sy):
            return None
        return best

    def find_best(self, names, screen: np.ndarray, threshold: float = 0.85,
                  region=None, scales=None):
        """多模板取最佳命中：names 可以是字符串或列表"""
        if isinstance(names, str):
            names = [names]
        best, best_name = None, None
        for n in names:
            hit = self.find(n, screen, threshold, region, scales)
            if hit and (best is None or hit[4] > best[4]):
                best, best_name = hit, n
        return (best_name, best) if best else (None, None)

    def find_all(self, name: str, screen: np.ndarray, threshold: float = 0.85,
                 max_count: int = 20):
        tpl = self._load(name)
        res = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
        th, tw = tpl.shape[:2]
        loc = np.where(res >= threshold)
        pts = sorted(zip(*loc[::-1]), key=lambda p: -res[p[1], p[0]])
        out = []
        for x, y in pts:
            if all(abs(x - ox) > tw * 0.6 or abs(y - oy) > th * 0.6 for ox, oy, *_ in out):
                out.append((int(x), int(y), tw, th, float(res[y, x])))
                if len(out) >= max_count:
                    break
        return out


def parse_color(c) -> tuple[int, int, int]:
    """'#RRGGBB' / [r,g,b] / 'r,g,b' → (b, g, r)"""
    if isinstance(c, str):
        s = c.strip().lstrip("#")
        if "," in s:
            r, g, b = [int(v) for v in s.split(",")[:3]]
        else:
            r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    else:
        r, g, b = [int(v) for v in c[:3]]
    return b, g, r


# ----------------------------------------------------------------------------
# 输入
# ----------------------------------------------------------------------------
class Input:
    def __init__(self, win: GameWindow, humanize: bool = True, click_jitter: int = 2,
                 dry_run: bool = False, ref_size=None):
        self.win = win
        self.humanize = humanize
        self.click_jitter = click_jitter
        self.dry_run = dry_run
        # ref_size=[W,H]：截图被归一化成这个尺寸后再识别（in_game.grab 会缩放），
        # 那么点击坐标也要按「实际客户区 / 参考尺寸」放大回去。
        # 换台分辨率更低的电脑时，所有写死的坐标与模板才能继续用（2026-11-17 18:32）。
        self.ref_size = ref_size
        self._held = set()
        pydirectinput.FAILSAFE = True      # 鼠标甩到左上角可紧急中断
        pydirectinput.PAUSE = 0

    # --- 抖动 ---
    def _j(self, v: float) -> float:
        if not self.humanize or v <= 0:
            return v
        return max(0.0, v * random.uniform(0.82, 1.22) + random.uniform(-0.008, 0.012))

    def _p(self, v: int) -> int:
        if not self.humanize:
            return v
        return v + random.randint(-self.click_jitter, self.click_jitter)

    # --- 键盘 ---
    def key(self, name: str, hold: float = 0.04, presses: int = 1, gap: float = 0.06):
        if self.dry_run:
            log.info(f"[dry] key {name} x{presses}")
            return
        try:
            self.win.ensure_foreground()
        except Exception:
            pass
        for i in range(max(1, presses)):
            pydirectinput.keyDown(name)
            time.sleep(self._j(hold))
            pydirectinput.keyUp(name)
            if i != presses - 1:
                time.sleep(self._j(gap))

    def key_down(self, name: str):
        if self.dry_run:
            log.info(f"[dry] keyDown {name}")
            return
        pydirectinput.keyDown(name)
        self._held.add(name)

    def key_up(self, name: str):
        if self.dry_run:
            log.info(f"[dry] keyUp {name}")
            return
        pydirectinput.keyUp(name)
        self._held.discard(name)

    def release_all(self):
        if self.dry_run:
            return
        for k in list(self._held):
            try:
                pydirectinput.keyUp(k)
            except Exception:
                pass
        self._held.clear()

    # --- 鼠标（客户区坐标） ---
    def _kxy(self):
        """归一化坐标 → 实际客户区坐标的倍率（ref_size 为空时 1.0，保持老行为）"""
        if not self.ref_size:
            return 1.0, 1.0
        try:
            _, _, cw, ch = self.win.client_rect()
            if cw > 0 and ch > 0:
                return cw / float(self.ref_size[0]), ch / float(self.ref_size[1])
        except Exception:
            pass
        return 1.0, 1.0

    def move(self, x: int, y: int):
        if self.dry_run:
            log.info(f"[dry] move {x},{y}")
            return
        try:
            self.win.ensure_foreground()
        except Exception:
            pass
        ox, oy, _, _ = self.win.client_rect()
        kx, ky = self._kxy()
        if kx != 1.0 or ky != 1.0:
            x, y = int(round(x * kx)), int(round(y * ky))
        pydirectinput.moveTo(ox + self._p(x), oy + self._p(y))

    def click(self, x: int, y: int, button: str = "left", hold: float = 0.05,
              clicks: int = 1, gap: float = 0.05):
        if self.dry_run:
            log.info(f"[dry] click {x},{y} {button} x{clicks}")
            return
        if _DBG is not None and _DBG.on():
            try:
                _DBG.on_click(self, x, y, clicks=clicks, button=button)
            except Exception:
                pass
        self.move(x, y)
        for _ in range(max(1, clicks)):
            pydirectinput.mouseDown(button=button)
            time.sleep(self._j(hold))
            pydirectinput.mouseUp(button=button)
            time.sleep(self._j(gap))

    def click_hover(self, x: int, y: int, hover: float = 0.8, hold: float = 0.12,
                    button: str = "left", clicks: int = 1, gap: float = 0.06):
        """先把鼠标停到目标上让 hover 生效，再按下。

        结算界面的「继续观战」直接 move+click 点不动（实测连点 3 次、匹配分数一直
        卡在 0.890 —— 那是 hover 高亮态），必须先 move 过去、停 0.6~0.8s 让 War3
        收到「鼠标进入按钮」的事件，再按下才生效。普通面板按钮用 click() 就够。
        """
        if self.dry_run:
            log.info(f"[dry] click_hover {x},{y} hover={hover}")
            return
        # Win7 那批机器渲染慢半拍，悬停要更久 War3 才认「鼠标进入」；
        # 倍数来自 configs/os_profile.json（默认 win7=1.25，其余 1.0）
        try:
            import sysenv as _se
            hover = hover * float(_se.hover_scale())
        except Exception:
            pass
        if _DBG is not None and _DBG.on():
            try:
                _DBG.on_click(self, x, y, clicks=clicks, button=button, hover=hover)
            except Exception:
                pass
        self.move(x, y)
        time.sleep(hover)
        for _ in range(max(1, clicks)):
            pydirectinput.mouseDown(button=button)
            time.sleep(self._j(hold))
            pydirectinput.mouseUp(button=button)
            time.sleep(self._j(gap))

    def drag(self, x1, y1, x2, y2, hold: float = 0.15):
        if self.dry_run:
            log.info(f"[dry] drag {x1},{y1}->{x2},{y2}")
            return
        self.move(x1, y1)
        pydirectinput.mouseDown(button="left")
        time.sleep(self._j(hold))
        self.move(x2, y2)
        time.sleep(self._j(0.1))
        pydirectinput.mouseUp(button="left")

    def wheel(self, x: int, y: int, times: int = 1, delta: int = -120,
              gap: float = 0.22):
        """在 (x,y) 处滚轮。delta<0 向下滚（看后面的行），>0 向上；120 为一格。
        pydirectinput 没有 scroll，这里直接发 MOUSEEVENTF_WHEEL。"""
        if self.dry_run:
            log.info(f"[dry] wheel {x},{y} x{times} delta={delta}")
            return
        self.move(x, y)
        time.sleep(0.15)
        u = ctypes.windll.user32
        for _ in range(max(1, times)):
            u.mouse_event(0x0800, 0, 0, ctypes.c_long(delta), 0)
            time.sleep(gap)

    # --- 文本输入（支持中文） ---
    def type_text(self, text: str, interval: float = 0.05, method: str = "unicode"):
        if self.dry_run:
            log.info(f"[dry] type {text!r} ({method})")
            return
        if method == "paste":
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            time.sleep(0.05)
            pydirectinput.keyDown("ctrl")
            pydirectinput.keyDown("v")
            time.sleep(0.03)
            pydirectinput.keyUp("v")
            pydirectinput.keyUp("ctrl")
        else:
            for ch in text:
                send_unicode_char(ch)
                time.sleep(self._j(interval))


# ----------------------------------------------------------------------------
# 流程控制异常
# ----------------------------------------------------------------------------
class StopBot(Exception):
    pass


class GameGone(Exception):
    """上一局已经彻底结束（War3 进程不在了）：等大厅没有任何意义，要重新点「开始游戏」。

    用户 2026-11-18 23:05：本机测试时手动结束了 War3 进程，平台弹「断线重连」，
    程序把弹窗关掉是对的，但接着又去等一个永远不会出现的大厅按钮，白耗 15 分钟。
    现在的规则：弹窗关掉 → 发现 War3 进程真的没了 → 立刻收手，交回 pipeline 重开一局。
    """


class RoomGone(GameGone):
    """房间窗已经确认不存在了（被关掉/平台重置）：回 ④ 已经没有意义，要从 ① 重新搜图建房。

    2026-11-27 16:31 用户定：断线弹窗收掉后如果房间窗确实没了（不是「截图读不到」，
    是窗口本身没了），就重新走「搜地图 → 创建房间」，而不是傻回 ④ 点一个不存在的按钮。
    次数由 pipeline.json 的 rebuild_on_room_gone 控制，防死循环。
    """


class PlatformRestarted(Exception):
    """KK 已重启，当前 pipeline 要从主页面重新开始。"""

class PlatformLoginRequired(Exception):
    """KK 重启后需要手动登录，当前 pipeline 必须停手。"""


_WAR3_EXES = ("war3.exe", "warcraft iii.exe")


def _pid_exe_name(pid: int) -> str:
    """按 pid 取进程文件名（Win32 API，零第三方依赖；成品里没有 psutil）"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value or "").lower()
            return ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def war3_alive() -> bool:
    """War3 **进程**还在不在。

    ⚠️ 窗口 0x0、窗口列表为空都**不代表**进程没了 —— 载入地图时窗口还没建出来
    （2026-11-18 17:04 那台电脑就是这样，等了 900 秒最后正常进游戏）。
    只有进程真的不存在，才能断定这一局结束了（手动结束任务 / 崩溃 / 秒退）。
    """
    try:
        import win32process
        pids = win32process.EnumProcesses()
    except Exception:
        return True                      # 判不了就当还活着（保守：继续等，绝不乱重开）
    for pid in pids:
        if pid and pid > 0 and _pid_exe_name(pid) in _WAR3_EXES:
            return True
    return False


class WindowLost(Exception):
    pass


# ----------------------------------------------------------------------------
# 机器人
# ----------------------------------------------------------------------------
STEP_KINDS = {
    "wait", "sleep", "log", "stop", "focus", "check_ad",
    "esc_safe",                    # 安全 ESC：屏幕上有「地图已更新」提示时跳过，避免把它关掉
    "key", "keys", "combo", "hotkey", "hold", "keydown", "keyup", "repeat_key", "key_until",
    "move", "click", "drag", "type", "write", "type_into",
    "assert_ink", "check_ink", "ensure_pwd",   # 2026-11-27 17:35：点「创建」前复核密码框真有内容
    "wait_image", "if_image", "wait_pixel", "if_pixel", "click_image",
    "click_last_row", "click_max_row", "click_row", "click_nth_row",
    "loop", "repeat",
    "start_app", "launch",        # 2026-11-20 02:39 用户要求：⑤ 点开始游戏时打开最小化工具
}

# 「真正对游戏产生动作」的步骤：task 里配了 step_gap 时，这些步骤执行完会自动停一下
ACTION_KINDS = {
    "key", "keys", "combo", "hotkey", "hold", "keydown", "keyup", "repeat_key",
    "move", "click", "drag", "type", "write", "type_into",
    "click_image", "click_last_row", "click_max_row", "click_row", "click_nth_row",
}


def detect_rows(img, region, bright=26, min_h=20, max_h=70, min_w=80):
    """在区域里识别「等距排列的行块」（难度列表、房间列表这类）。
    返回 [(top, bottom, left, right), ...]，坐标是整幅图的绝对坐标，按 y 从上到下排序。"""
    x0, y0, rw, rh = [int(v) for v in region]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)[y0:y0 + rh, x0:x0 + rw]
    mask = (gray > bright).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    num, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    rows = []
    for i in range(1, num):
        x, y, w, h, area = stats[i]
        if w < min_w or not (min_h <= h <= max_h):
            continue
        rows.append((y + y0, y + y0 + h, x + x0, x + x0 + w))
    rows.sort()
    return rows


def grid_complete_rows(img, rows, region, bright=26, min_ratio=0.10, max_rows=20):
    """按等距网格把「被高亮行吃掉」的行补回来，返回补全后的行列表。

    为什么需要：选中那一行的边框会上下各多画约 15px（实测普通行 h=44，高亮行 h=59），
    于是它和下一行在二值图里粘成一块（h≈98），超过 max_h 被整块过滤 ——
    行数直接少一行（实测 9 行变 8 行）。当大厅出现 N10 时，N9 正好是选中行，
    就会连 N10 一起被吞，最后「点最高那一行」点到 N8 上去。

    做法：用已检出行的高/间距推断 pitch 与首行 top，沿网格逐行看「这一格有没有内容」
    （行内亮度 > bright 的像素占比），有就补一行。只补不删，原检出行原样保留。
    """
    if not rows:
        return rows
    rows = sorted(rows)
    tops = [t for (t, b, l, r) in rows]
    hs = [b - t for (t, b, l, r) in rows]
    cand_h = [h for h in hs if h <= 60] or hs
    try:
        row_h = int(np.median(cand_h))
    except Exception:
        return rows
    if row_h < 10:
        return rows
    diffs = [tops[i + 1] - tops[i] for i in range(len(tops) - 1)]
    if not diffs:
        return rows
    dmin = min(diffs)
    near = [d for d in diffs if d <= dmin * 1.4]
    try:
        pitch = int(round(float(np.median(near))))
    except Exception:
        return rows
    if pitch < 20:
        return rows
    l = int(np.median([l for (t, b, l, r) in rows]))
    r = int(np.median([r for (t, b, l, r) in rows]))
    x0, y0, rw, rh = [int(v) for v in region]
    top0 = tops[0]
    have = set(int(round((t - top0) / float(pitch))) for (t, b, l2, r2) in rows)
    out = list(rows)
    added = []
    for k in range(max_rows + 1):
        top = top0 + k * pitch
        if top + row_h > y0 + rh:
            break
        if k in have:
            continue
        sub = img[max(0, top):top + row_h, l:r]
        if not sub.size:
            continue
        try:
            g = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
            ratio = float((g > bright).mean())
        except Exception:
            continue
        if ratio >= min_ratio:
            out.append((top, top + row_h, l, r))
            added.append(k + 1)
    if added:
        log.info(f"   ↳ 网格补回被高亮吃掉的行：第 {added} 行"
                 f"（高亮行 h 比普通行大，会和下一行粘成一块被漏检）")
    return sorted(out)


def fit_spec(kw, region, img, ref):
    """把「按参考客户区尺寸写死的识别参数」等比换算到当前窗口。

    参考尺寸 ref=[W,H] 是当初调参时那个窗口的大小（例如大厅 1690x921）。
    换台电脑/换了窗口大小后，写死的 region、行高阈值、fallback 行距全部错位：
    2026-11-17 18:32 用户实测，窗口缩到约 0.63 倍时一行都识别不到 → 卡在选难度界面。
    返回 (kw2, region2, sx, sy)；ref 为空时原样返回，sx=sy=1.0。
    """
    if not ref:
        return kw, region, 1.0, 1.0
    try:
        rw_ref, rh_ref = float(ref[0]), float(ref[1])
        if rw_ref <= 0 or rh_ref <= 0:
            return kw, region, 1.0, 1.0
    except Exception:
        return kw, region, 1.0, 1.0
    h, w = img.shape[:2]
    sx, sy = w / rw_ref, h / rh_ref
    kw2 = dict(kw)
    for k in ("min_h", "max_h"):          # 行高按窗口高度缩放
        if k in kw2:
            kw2[k] = max(1, int(round(kw2[k] * sy)))
    if "min_w" in kw2:                    # 行宽按窗口宽度缩放
        kw2["min_w"] = max(1, int(round(kw2["min_w"] * sx)))
    region2 = region
    if region:
        region2 = [int(round(region[0] * sx)), int(round(region[1] * sy)),
                   max(1, int(round(region[2] * sx))), max(1, int(round(region[3] * sy)))]
    return kw2, region2, sx, sy


DIFF_TARGET = os.path.join("logs", "diff_target.json")
DIFF_DIGITS = os.path.join("templates", "diff_digits")
DIFF_DUMP = os.path.join("logs", "diff_rows")

# 宿主机对项目目录里「已存在的文件」拒绝覆盖（Errno 13 / trash 失败）。
# 难度记录和难度裁图都是「非关键写入」，失败就落到 %TEMP%/KKAuto/，别刷一屏 warning。
ALT_DIR = os.path.join(os.environ.get("TEMP") or os.environ.get("TMPDIR") or ".", "KKAuto")


def alt_path(*parts):
    """项目目录写不进去时的兜底路径（%TEMP%/KKAuto/...）。"""
    p = os.path.join(ALT_DIR, *parts)
    d = os.path.dirname(p)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return p


def dump_json_any(path, rec):
    """先写项目目录，失败再写 %TEMP% 兜底；返回实际写入路径（都失败返回 None）。"""
    import json as _json
    for p in (path, alt_path(os.path.basename(path))):
        try:
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                _json.dump(rec, f, ensure_ascii=False, indent=2)
            return p
        except Exception:
            continue
    return None


def read_diff_target():
    """读「本轮该选哪个难度」——由 full_loop 在 A 阶段前写入。

    返回 (行号 1 基, 名字, 标签) ；读不到返回 (None, None, None)。
    这样不用改配置文件就能逐轮换难度（6 局分别 N1..N6）。
    """
    try:
        import json as _json
        with open(DIFF_TARGET, encoding="utf-8") as f:
            j = _json.load(f)
        row = int(j.get("row") or 0)
        return (row if row > 0 else None), j.get("name"), (j.get("tag") or "lobby")
    except Exception:
        return None, None, None


def row_center(rows, idx):
    """取 rows[idx]（0 基）的中心点。

    detect_rows 的 bbox 会被「选中行高亮」撑大（实测行高 44 → 59），
    直接取 bbox 中点会偏几像素，所以按**行距**推算：只用「高度正常」的行
    做一条 行号→y 的直线，再取第 idx 行的值（高亮行自己的 bbox 不参与拟合）。
    """
    if not rows:
        return None
    idx = max(0, min(idx, len(rows) - 1))
    centers = [(t + b) // 2 for (t, b, l, r) in rows]
    hs = [b - t for (t, b, l, r) in rows]
    if len(centers) < 2:
        top, bot, left, right = rows[idx]
        return (left + right) // 2, centers[idx]

    # 正常行 = 高度最小的那一批（高亮行会被撑高 10~15px）
    hnorm = min(hs)
    norm = [j for j in range(len(rows)) if hs[j] <= hnorm + 3]
    if len(norm) >= 2:
        n = len(norm)
        sx = sum(norm)
        sy = sum(centers[j] for j in norm)
        sxx = sum(j * j for j in norm)
        sxy = sum(j * centers[j] for j in norm)
        den = n * sxx - sx * sx
        pitch = (n * sxy - sx * sy) / den if den else 0.0
        base = (sy - pitch * sx) / n
        cy = base + pitch * idx if abs(pitch) >= 3 else centers[idx]
    else:
        pitch = centers[-1] - centers[0]
        pitch = pitch / (len(centers) - 1) if len(centers) > 1 else 0
        cy = centers[0] + pitch * idx

    # 不许偏离自己 bbox 中心超过半行，避免拟合出来的点跳到邻行
    half = max(6, int(abs(pitch) / 2))
    cy = max(centers[idx] - half, min(centers[idx] + half, int(round(cy))))
    top, bot, left, right = rows[idx]
    return (left + right) // 2, cy


def highlight_index(rows):
    """选中行会被高亮撑高 → 返回最高的那一行（0 基）；分辨不出来返回 None。"""
    if len(rows) < 2:
        return None
    hs = [b - t for (t, b, l, r) in rows]
    mx, mn = max(hs), min(hs)
    if mx - mn < max(3, mn // 5):        # 行高差太小，判不出来
        return None
    return hs.index(mx)


def gold_highlight_index(img, rows):
    """按「金色高亮边框」判选中行（0 基），判不出返回 None。

    比 highlight_index（行高差）稳：选中行的边框是金黄色，未选中的是暗灰，
    金色像素几乎只出现在选中行（2026-11-18 实测：N8 选中 599 个金像素，其他行 0 个）。
    行高差在「高亮行被画得更高」时能被行检测吞掉（实测 9 行变成 8 行），金色判据不受影响。
    """
    if not rows:
        return None
    scores = []
    for (t, b, l, r) in rows:
        t, b, l, r = int(t), int(b), int(l), int(r)
        t, b = max(0, t), min(img.shape[0], b)
        l, r = max(0, l), min(img.shape[1], r)
        if b - t < 8 or r - l < 20:
            scores.append(0)
            continue
        sub = img[t:b, l:r].astype("int16")
        bch, gch, rch = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
        m = (rch > 110) & (gch > 80) & (bch < rch - 40)
        scores.append(int(m.sum()))
    mx = max(scores)
    if mx < 60:                     # 金像素太少，认为这一帧没有高亮行
        return None
    order = sorted(scores, reverse=True)
    if len(order) > 1 and order[1] > mx * 0.5:   # 两行都像高亮 → 判不出
        return None
    return scores.index(mx)


def read_digit(img, row, tpl_dir=DIFF_DIGITS):
    """图色识别某一行里的难度数字（N1~N9 的阿拉伯数字部分）。

    判据：行内亮度更高的一小块才是字（行背景比字暗），取字块 bbox → 归一化成
    28×28 灰度 → 和 templates/diff_digits/nX.png 逐个比对，取最高分。
    模板不存在就返回 (None, 0.0)，由调用方记「无模板、跳过文字校验」。
    返回 (数字, 分数)。
    """
    t, b, l, r = [int(v) for v in row]
    h, w = img.shape[:2]
    t, b = max(0, t), min(h, b)
    l, r = max(0, l), min(w, r)
    if b - t < 8 or r - l < 20:
        return None, 0.0
    sub = cv2.cvtColor(img[t:b, l:r], cv2.COLOR_BGR2GRAY)
    # 行背景偏暗、字偏亮：先取一个能剥出字的高阈值
    for thr in (150, 120, 170, 100, 190):
        mask = (sub > thr).astype(np.uint8)
        if mask.sum() < 30:
            continue
        n, _, st, _ = cv2.connectedComponentsWithStats(mask, 8)
        cand = [i for i in range(1, n) if st[i, 4] >= 12 and st[i, 3] >= 8]
        if not cand:
            continue
        x0 = min(st[i, 0] for i in cand)
        y0 = min(st[i, 1] for i in cand)
        x1 = max(st[i, 0] + st[i, 2] for i in cand)
        y1 = max(st[i, 1] + st[i, 3] for i in cand)
        if x1 - x0 < 6 or y1 - y0 < 8:
            continue
        glyph = sub[y0:y1, x0:x1]
        scores = {}
        files = sorted(os.listdir(tpl_dir)) if os.path.isdir(tpl_dir) else []
        for f in files:
            m = re.match(r"n?(\d+)", os.path.splitext(f)[0])
            if not m:
                continue
            tpl = cv2.imread(os.path.join(tpl_dir, f), cv2.IMREAD_GRAYSCALE)
            if tpl is None:
                continue
            g = cv2.resize(glyph, (tpl.shape[1], tpl.shape[0]), interpolation=cv2.INTER_AREA)
            r_ = cv2.matchTemplate(g, tpl, cv2.TM_CCOEFF_NORMED)
            v = float(r_.max())
            k = int(m.group(1))
            if v > scores.get(k, 0.0):
                scores[k] = v
        if not scores:
            log.info("      （templates/diff_digits 里还没有模板）")
            return None, 0.0
        rank = sorted(scores.items(), key=lambda kv: -kv[1])
        if len(rank) < 2:
            # 只有 1 个模板时任何按钮都会匹配到它 → 没法判，别乱报
            log.info(f"      （只有 n{rank[0][0]} 一个模板，{rank[0][1]:.3f}，无法区分）")
            return None, round(rank[0][1], 3)
        if rank[0][1] - rank[1][1] < 0.04:
            log.info(f"      （n{rank[0][0]}={rank[0][1]:.3f} 与 n{rank[1][0]}={rank[1][1]:.3f} "
                     f"分数太接近，判不出）")
            return None, round(rank[0][1], 3)
        return rank[0][0], round(rank[0][1], 3)
    n_tpl = len([f for f in os.listdir(tpl_dir)
                 if f.lower().endswith(".png")]) if os.path.isdir(tpl_dir) else 0
    if n_tpl == 0:
        log.info("      （templates/diff_digits 里还没有模板）")
    else:
        log.info(f"      （有 {n_tpl} 个模板，但这行剥不出字形 → 判不出数字）")
    return None, 0.0


def imwrite_cn(path, img) -> bool:
    """写图到磁盘 —— 中文路径安全版

    2026-11-18 15:46：`cv2.imwrite` 在含中文的路径下（项目目录叫「我在峡谷打」）
    **静默返回 False、不抛异常**，所以「学难度字形模板」「存难度列表裁图」这些
    一直在无声失败，只是没人注意。统一走 imencode + ndarray.tofile。
    """
    try:
        ext = os.path.splitext(path)[1] or ".png"
        ok, buf = cv2.imencode(ext, img)
        if not ok:
            return False
        buf.tofile(path)
        return True
    except Exception:
        return False


def dump_rows(img, rows, tag="r", region=None):
    """把难度列表整幅 + 每行单独裁图落盘，供事后核对（也给「学数字模板」用）。

    项目目录写不进去（宿主机写保护）时，自动改写到 %TEMP%/KKAuto/diff_rows/。
    """
    for dump_dir in (DIFF_DUMP, alt_path("diff_rows")):
        try:
            os.makedirs(dump_dir, exist_ok=True)
            ok = imwrite_cn(os.path.join(dump_dir, f"{tag}_full.png"), img)
            if not ok:
                continue
            for i, (t, b, l, r) in enumerate(rows):
                pad = 2
                crop = img[max(0, t - pad):b + pad, max(0, l - pad):r + pad]
                imwrite_cn(os.path.join(dump_dir, f"{tag}_row{i + 1}.png"), crop)
            with open(os.path.join(dump_dir, f"{tag}_rows.txt"), "w", encoding="utf-8") as f:
                f.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')}  region={region}\n")
                for i, (t, b, l, r) in enumerate(rows):
                    f.write(f"row{i + 1}\ttop={t}\tbot={b}\th={b - t}\tleft={l}\tright={r}\tw={r - l}\n")
            log.info(f"难度列表已落盘：{dump_dir}/{tag}_*.png（{len(rows)} 行）")
            return
        except Exception as e:
            log.debug(f"落盘到 {dump_dir} 失败：{e}")
    log.warning("落盘难度列表失败（项目目录与临时目录都写不进）")


def glyph_of(gray):
    """从行/按钮截图里剥出「文字块」：Otsu 二值 → 取连通区外接框 → 返回灰度字形裁剪。"""
    if gray is None or gray.size == 0:
        return None
    h, w = gray.shape[:2]
    # 按钮边框是暗黄（120~160），字是近白（200+）。Otsu 会把边框也算进来，
    # 所以用绝对阈值把白字剥出来（17:35 实测：Otsu 剥出 155x41 = 整颗按钮）。
    for thr in (200, 185, 170):
        m = (gray > thr).astype(np.uint8)
        ys, xs = np.where(m > 0)
        if len(xs) < 20:
            continue
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        gw, gh = x1 - x0, y1 - y0
        if gw < 8 or gh < 8:
            continue
        if gh > h * 0.85 or gw > w * 0.9:      # 还是把整块框进来了，换更高阈值
            continue
        return gray[y0:y1, x0:x1]
    return None


def save_btn_crop(img, row, tag, learn_as=None):
    """把点到的难度按钮裁图存盘，并和上一轮的比一比：难度到底有没有推进。

    换账号后大厅只显示「当前可打的那一个难度」，所以**没法靠行号推编号**，
    「这一轮的按钮和上一轮长得一样吗」就是唯一能自证进度推进的判据。
    learn_as: 已知编号时把字形存成 templates/diff_digits/nX.png（方案 3 的模板）。
    返回 (相似度 或 None, 上一轮标签)
    """
    try:
        t, b, l, r = [int(v) for v in row]
        sub = img[max(0, t):b, max(0, l):r]
        if sub.size == 0:
            return None, None
        g = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
        os.makedirs(DIFF_DUMP, exist_ok=True)
        cur = os.path.join(DIFF_DUMP, f"btn_{tag}.png")
        imwrite_cn(cur, g)
        gl = glyph_of(g)
        if gl is None:
            return None, None
        # 先和最近一张（别的 tag）比字形，判「难度有没有推进」
        sim, p_tag = None, None
        prevs = [f for f in os.listdir(DIFF_DUMP)
                 if f.startswith("btn_") and f != os.path.basename(cur)]
        if prevs:
            prevs.sort(key=lambda f: os.path.getmtime(os.path.join(DIFF_DUMP, f)))
            p_tag = prevs[-1][4:-4]
            p_img = cv2.imread(os.path.join(DIFF_DUMP, prevs[-1]), cv2.IMREAD_GRAYSCALE)
            p_g = glyph_of(p_img)
            if p_g is not None:
                a = cv2.resize(gl, (40, 24), interpolation=cv2.INTER_AREA)
                b_ = cv2.resize(p_g, (40, 24), interpolation=cv2.INTER_AREA)
                sim = float(cv2.matchTemplate(a, b_, cv2.TM_CCOEFF_NORMED)[0][0])

        # 学模板：只有「确认推进了」（和上一轮明显不同）才学，免得把 N1 存成 n2
        if learn_as and (sim is None or sim < 0.93):
            os.makedirs(DIFF_DIGITS, exist_ok=True)
            dst = os.path.join(DIFF_DIGITS, f"n{int(learn_as)}.png")
            if not os.path.exists(dst):
                imwrite_cn(dst, gl)
                log.info(f"   📚 学到难度字形模板 templates/diff_digits/n{int(learn_as)}.png"
                         f"（{gl.shape[1]}x{gl.shape[0]}）")
        return sim, p_tag
    except Exception as e:
        log.warning(f"按钮裁图比对失败（不影响）：{e!r}")
        return None, None


def last_row_center(rows):
    """取最下面那一行的中心点。行高被高亮撑大时按行距推算，避免落到下一行或线上。"""
    top, bot, left, right = rows[-1]
    centers = [(t + b) // 2 for (t, b, l, r) in rows]
    if len(centers) >= 2:
        diffs = sorted(centers[i + 1] - centers[i] for i in range(len(centers) - 1))
        pitch = diffs[len(diffs) // 2]
        cy = centers[-2] + pitch
        cy = max(top, min(bot, cy))
    else:
        cy = centers[-1]
    return (left + right) // 2, cy


class Bot:
    def __init__(self, cfg: dict, cfg_dir: str, dry_run: bool = False):
        self.cfg = cfg
        self.cfg_dir = cfg_dir
        self.dry_run = dry_run
        self.win_cfg = cfg.get("window", {})
        # 当前正在等哪张图（wait_image 进来时写、出去时清）。窗口中途失效时，
        # _checkpoint 拿它能「按画面内容把窗口找回来」，不用再靠句柄/尺寸猜。
        self._wait_spec = None
        # wait_window：窗口还没起来时（比如点了开始游戏、War3 正在启动）先等它
        wait_sec = float(cfg.get("wait_window", 0) or 0)
        t0 = time.time()
        last_diag = 0.0
        while True:
            try:
                self.win = GameWindow(
                    title_re=self.win_cfg.get("title_re"),
                    exe=self.win_cfg.get("exe"),
                    min_size=int(self.win_cfg.get("min_size", 200)),
                    pick=self.win_cfg.get("pick", "largest"),
                    size_hint=self.win_cfg.get("size_hint"),
                )
                break
            except Exception as e:
                waited = time.time() - t0
                if waited >= wait_sec:
                    raise
                # 每 5 秒把「真实原因 + 当前可见窗口清单」打一次。
                # 2026-11-17 19:11 用户现场：只看到一直「等窗口出现」，完全不知道是
                # 进程名不对、标题不对，还是窗口被最小化 —— 原因被这里吞掉了。
                if waited - last_diag >= 5:
                    last_diag = waited
                    log.warning(f"等窗口出现（已等 {int(waited)}s）原因：{e}")
                    log.warning("   " + window_report(6))
                else:
                    log.warning(f"等窗口出现：exe={self.win_cfg.get('exe')}"
                                f"（已等 {int(waited)}s）")
                time.sleep(1.0)
        self.cap = Capture(cfg.get("capture", "auto"))
        self.vis = Vision(self._resolve(cfg.get("template_dir", "templates")))
        self.humanize = bool(cfg.get("humanize", True))
        self.input = Input(self.win, self.humanize,
                           int(cfg.get("click_jitter", 2)), dry_run)
        self._run_flag = threading.Event()
        self._paused = threading.Event()
        self._paused.set()          # set = 可运行
        self._stop = threading.Event()
        self._started = threading.Event()
        self._current_task = None
        self._step_gap = float(cfg.get("step_gap", 0) or 0)   # 动作步骤之间的默认间隔(ms)
        self._last_fg = 0.0
        # 模板基准尺寸（大厅配置写 [1690, 921]）：窗口和它不一致时，
        # 额外按 (宽/ref_w, 高/ref_h) 试一档缩放，解决「横向压扁」的窗口
        self._cap_ref = cfg.get("cap_ref")
        # 窗口消失后给它几秒找回（War3 进图会重建窗口）；0 = 不找回（默认，旧行为）
        self._last_dead_log = 0.0   # 「窗口暂时不在了」这行日志的节流时间戳

    def _resolve(self, p: str) -> str:
        if os.path.isabs(p):
            return p
        cand = os.path.join(self.cfg_dir, p)
        if os.path.exists(cand):
            return cand
        alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
        return alt if os.path.exists(alt) else cand

    # --- 状态 ---
    def _checkpoint(self):
        """步骤之间调用：处理暂停/停止/窗口丢失/失焦"""
        if self._stop.is_set():
            raise StopBot()
        now = time.time()
        if not self.win.alive():
            # 窗口没了怎么办（2026-11-18 18:49 用户要求「只留按画面绑定」）：
            #   · 正在等某张图（any_window）→ 谁画出这张图就绑谁，找不着就继续等；
            #     这里**不判死**，因为 War3 进图重建窗口期间本来就什么都没有。
            #   · 其它步骤 → 直接判「游戏关了」，不再玩「同进程/尺寸猜窗口」那套。
            spec = getattr(self, "_wait_spec", None)
            if spec and spec.get("any_window"):
                if self._bind_by_content(spec):
                    log.warning("   窗口换了，但画面已经出现 → 直接接着用新窗口")
                elif now - self._last_dead_log > 3.0:
                    self._last_dead_log = now
                    log.info("   …窗口暂时不在了（War3 正在重建/载入），继续按画面找")
            else:
                raise WindowLost("游戏窗口已关闭")
        # 3 秒节流，防止频繁抢前台。
        # 例外：_quiet_focus_until 未过期时不抢（等大厅的静默期，War3 正在启动/载入，
        # 反复 SetForegroundWindow 会打断它 —— 用户 2026-11-26 选 B）。
        if now - self._last_fg > 3.0 \
                and now >= getattr(self, "_quiet_focus_until", 0.0):
            self._last_fg = now
            if not self.win.fg_same_process():
                log.warning("游戏窗口不在前台，自动切回")
                self.win.focus()
        while not self._paused.is_set():
            if self._stop.is_set():
                raise StopBot()
            time.sleep(0.05)

    def _sleep(self, ms: float):
        end = time.time() + ms / 1000.0
        while time.time() < end:
            self._checkpoint()
            time.sleep(min(0.05, max(0.001, end - time.time())))

    # --- 热键 ---
    def bind_hotkeys(self):
        hk = self.cfg.get("hotkeys", {})
        k_start = hk.get("start_pause", "f8")
        k_stop = hk.get("stop", "f9")
        k_panic = hk.get("panic", "f10")

        def toggle():
            if not self._started.is_set():
                self._started.set()
                log.info("[热键] 开始执行")
                return
            if self._paused.is_set():
                self._paused.clear()
                log.info("[热键] 已暂停")
            else:
                self._paused.set()
                log.info("[热键] 继续运行")

        def stop():
            self._stop.set()
            self._paused.set()
            log.info("[热键] 收到停止指令")

        def panic():
            self._stop.set()
            self._paused.set()
            self.input.release_all()
            log.warning("[热键] 紧急停止，已释放所有按键")

        keyboard.add_hotkey(k_start, toggle, suppress=False)
        keyboard.add_hotkey(k_stop, stop, suppress=False)
        keyboard.add_hotkey(k_panic, panic, suppress=False)
        log.info(f"热键已绑定：{k_start.upper()} 开始/暂停  "
                 f"{k_stop.upper()} 停止  {k_panic.upper()} 紧急释放")

    # --- 任务执行 ---
    def _region_patch(self, region):
        """取指定区域的图像（BGR）；抓不到返回 None。"""
        x, y, w, h = region
        try:
            img = self.cap.grab(self.win)
        except Exception:
            return None
        if img is None or not getattr(img, "size", 0):
            return None
        patch = img[y:y + h, x:x + w]
        return patch if patch.size else None

    def _region_std(self, region):
        """取指定区域的像素方差，用来判断输入框里是否出现了内容"""
        patch = self._region_patch(region)
        return float(patch.std()) if patch is not None else 0.0

    @staticmethod
    def _patch_diff_ratio(a, b):
        """两张同尺寸图的「明显变化像素」占比（0~1）。

        2026-11-27 17:35 现场：W10 上「房间密码」框空着，方差也从 8.46 变到 13.56
        （光标闪烁/重绘），旧判据只看方差就判「输入生效」→ 点「创建」→ 开成无密码房。
        这里按像素比一次，只有真的出现字符/圆点（占比够高）才算写进去了。
        """
        if a is None or b is None or a.shape != b.shape:
            return 0.0
        try:
            d = cv2.absdiff(a, b).max(axis=2)
            return float((d > 40).sum()) / float(d.size)
        except Exception:
            return 0.0

    @staticmethod
    def _patch_ink(patch):
        """图里「明显不同于底色」的像素数（密码框里的圆点 / 字符）。

        以区域灰度中位值当底色，差 >110 的算内容。深色主题（深底白点）和浅色主题
        （白底黑字）都适用；空框里那行灰色占位提示（差 ~80）不会被算成内容。
        """
        if patch is None or not getattr(patch, "size", 0):
            return 0
        try:
            gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.int16)
            bg = int(np.median(gray))
            return int((np.abs(gray - bg) > 110).sum())
        except Exception:
            return 0

    def _cancel_and_reopen_dialog(self, spec) -> bool:
        """点「取消」关掉建房弹窗 → 回详情页点「创建房间」→ 等弹窗重开。

        2026-11-27 17:48 用户要求：密码框补输 3 次还空着，不要直接判失败，
        先点「取消」把弹窗关掉，再重新点「创建房间」开一个干净的弹窗重填。
        弹窗销毁后 Bot 绑的还是那扇死窗，所以截图改从平台主窗（1332x945）拿。
        """
        cx, cy = spec.get("cancel_point", [349, 411])
        for _ in range(2):
            try:
                self.input.click(int(cx), int(cy))
            except Exception as e:
                log.warning(f"   点「取消」出错：{type(e).__name__}: {e}")
            self._sleep(800)
            if not create_dialog_hwnd():
                log.info("   已点「取消」，建房弹窗已关闭")
                break
        main = None
        try:
            import room_finder as _rf
            for w in _rf.platform_windows(min_client=300):
                if (1250 <= w["client"][2] <= 1400
                        and 900 <= w["client"][3] <= 1000):
                    main = w
                    break
        except Exception:
            main = None
        px, py = spec.get("create_point", [814, 893])
        if main is not None:
            try:
                import plat_home as _ph
                img = _ph.pg.grab_hwnd(main["hwnd"])
                tpl_path = self._resolve("templates/btn_create_room.png")
                if (img is not None and getattr(img, "size", 0)
                        and os.path.exists(tpl_path)):
                    tpl = cv2.imdecode(np.fromfile(tpl_path, dtype=np.uint8),
                                       cv2.IMREAD_COLOR)
                    if tpl is not None:
                        r = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
                        _mn, mx, _mnl, mxl = cv2.minMaxLoc(r)
                        if mx >= 0.80:
                            px = mxl[0] + tpl.shape[1] // 2
                            py = mxl[1] + tpl.shape[0] // 2
                            log.info(f"   详情页「创建房间」命中 {mx:.3f} @({px},{py})")
                        else:
                            log.info(f"   详情页「创建房间」没认到（{mx:.3f}）→ 用固定坐标 ({px},{py})")
            except Exception as e:
                log.info(f"   找「创建房间」按钮出错（用固定坐标）：{type(e).__name__}: {e}")
        # 弹窗已销毁，self.input 还绑在那扇死窗上（client_rect 会算错原点），
        # 所以这里按**主窗**的客户区原点自己换算屏幕坐标。
        try:
            import win32gui
            import pydirectinput
            if main is not None:
                ox, oy = win32gui.ClientToScreen(main["hwnd"], (0, 0))
            else:
                ox, oy = 0, 0
            pydirectinput.moveTo(int(ox + px), int(oy + py))
            time.sleep(0.08)
            pydirectinput.mouseDown()
            time.sleep(0.06)
            pydirectinput.mouseUp()
            log.info(f"   已点详情页「创建房间」屏幕坐标 ({int(ox + px)},{int(oy + py)})")
        except Exception as e:
            log.warning(f"   点「创建房间」出错：{type(e).__name__}: {e}")
        t0 = time.time()
        while time.time() - t0 < 8:
            h = create_dialog_hwnd()
            if h:
                # 旧弹窗已销毁；后续截图和输入必须绑定新句柄。
                new_win = GameWindow(
                    title_re=self.win_cfg.get("title_re"),
                    exe=self.win_cfg.get("exe"),
                    min_size=int(self.win_cfg.get("min_size", 200)),
                    pick=self.win_cfg.get("pick", "largest"),
                    size_hint=self.win_cfg.get("size_hint"),
                )
                if new_win.hwnd == h:
                    self.win = new_win
                    self.input.win = new_win
                    log.info(f"   ✓ 建房弹窗已重开并绑定（hwnd={h}）→ 重新填密码")
                    self._sleep(700)
                    return True
                log.warning(f"   建房弹窗句柄 {h} 与重新绑定的窗口 {new_win.hwnd} 不一致")
                return False
            self._sleep(300)
        log.warning("   ⚠️ 8s 内没等到建房弹窗重开")
        return False

    def _scales_for(self, spec, cw: float, ch: float) -> list:
        """按「某个窗口的客户区宽高」算缩放档（模板是在 ref 尺寸下截的）"""
        scales = list(spec.get("scales") or [1.0])
        ref = spec.get("ref") or self._cap_ref
        if ref and len(ref) == 2 and cw > 0 and ch > 0:
            fx, fy = cw / float(ref[0]), ch / float(ref[1])
            if abs(fx - 1) > 0.02 or abs(fy - 1) > 0.02:
                pair = (round(fx, 3), round(fy, 3))
                if pair not in scales:
                    # 插到第 2 位（2026-11-19）：这一档是「按当前窗口算出来的真答案」，
                    # 比配置里 0.95/0.9/0.85 那些防御档准得多。排前面之后正常命中
                    # 只需跑 2 档；0.5x 粗筛也才能只靠「1.0 + 这一档」判断有没有。
                    head_is_1 = (bool(scales) and isinstance(scales[0], (int, float))
                                 and abs(scales[0] - 1.0) < 1e-6)
                    if head_is_1:
                        scales.insert(1, pair)
                    else:
                        scales.insert(0, pair)
        return scales

    def _bind_by_content(self, spec, min_gap: float = 2.0) -> bool:
        """**按画面内容找窗口**：扫所有像游戏窗的窗口，谁画出了这张图就绑谁。

        为什么（2026-11-18 18:43 用户的主意）：
          War3 进图会重建窗口，标题变成地图名、句柄作废，之前靠「同进程 / 配置规则 /
          尺寸接近」三层猜，全是间接证据。而「画面里出现了大厅开始按钮」是**直接证据**：
          能画出这个画面的窗口，就是我们要操作的那个窗口，不用管它换了几个句柄。
        代价：一次要抓 2~3 个窗口做模板匹配（约 100~200ms），所以只在两种时机做：
          a) 绑定的窗口失效时（_checkpoint）
          b) 开了 any_window 的等待步骤里，每 30 秒扫一次（防止「旧窗口还活着但画面没了」）
             （2026-11-19 17:14 用户定 15 秒、17:44 改 30 秒：原来 1 秒一次，实测单次扫描
              抓图 100ms + 匹配 41ms ≈ 167ms，2 秒节流下等于常驻吃掉 8.4% 单核；
              War3 重建窗口本来就要好几秒，30 秒一次开销约 0.6%）
        """
        # 节流（2026-11-19 降 CPU）：一次扫描最多试 4 个窗口 × 多档匹配 ≈ 1.2 秒，
        # 而调用方有两处会高频进来（_checkpoint 每 50ms 一次、等图循环每 1 秒一次）。
        # 原来窗口一消失就等于 100% 占一个核在反复扫；2 秒一次不影响
        # 「War3 重建窗口后接上」的时效（它载入本来就要好几秒）。
        _now_b = time.time()
        if _now_b - getattr(self, "_bind_last", 0.0) < min_gap:
            return False
        self._bind_last = _now_b
        tpls = spec.get("template")
        if not tpls:
            return False
        th = float(spec.get("threshold", 0.85))
        try:
            cur = self.win.hwnd
        except Exception:
            cur = 0
        # 先看当前已绑窗口（最可能），再看其它候选
        cands = []
        for w in enum_windows_full():
            if not is_game_candidate(w, int(getattr(self.win, "min_size", 400) or 400)):
                continue
            cands.append(w)
        cands.sort(key=lambda x: 0 if x["hwnd"] == cur else 1)
        for w in cands[:4]:        # 最多试 4 个，避免拖慢主循环
            shim = _WinShim(w["hwnd"], (0, 0, w["w"], w["h"]))
            try:
                img = self.cap.grab(shim)
            except Exception:
                continue
            if img is None or getattr(img, "size", 0) == 0:
                continue
            name, hit = self.vis.find_best(tpls, img, th,
                                           None,     # region 是「已绑窗口」的布局坐标，换窗口不适用
                                           self._scales_for(spec, w["w"], w["h"]))
            if hit:
                changed = w["hwnd"] != cur
                self.win.hwnd = w["hwnd"]
                self.win.pid = w["pid"] or getattr(self.win, "pid", 0)
                if changed:
                    log.warning(f"✅ 按画面内容绑定窗口: hwnd={w['hwnd']} "
                                f"exe={w.get('exe')!r} title={w['title']!r} "
                                f"客户区={w['w']}x{w['h']} "
                                f"（{name} score={hit[4]:.3f}）")
                return True
        return False

    def _room_state(self):
        """断线弹窗收掉后，房间窗现在是什么状态。

        返回：
          "ready"  → 房间在 + 「开始游戏」亮着（score ≥ START_SCORE）→ 回 ④ 重开一局
          "busy"   → 房间在，但「开始游戏」没亮（平台仍认为在游戏中）→ 继续等
          "gone"   → **确认房间窗已经没了**（窗口本身不存在）→ 重新 ①②③ 建房
          "unknown"→ 判不了（截图读不到等）→ 继续等，不乱动
        """
        try:
            import room_finder as rf
        except Exception:
            return "unknown"
        th = float(getattr(rf, "START_SCORE", 0.72) or 0.72)
        hwnd = (BOUND_ROOM or {}).get("hwnd") or 0
        if hwnd:
            # 优先看已绑定的那扇：窗口对象都没了 = 房间确实被关掉了（不是截图读不到）
            try:
                import win32gui
                if not win32gui.IsWindow(hwnd):
                    log.info(f"   已绑定房间窗 hwnd={hwnd} 已不存在")
                    return "gone"
            except Exception:
                pass
            try:
                pk = rf.find_room_by_hwnd(hwnd, activate_first=True)
                if pk:
                    sc = float(pk[1] or 0)
                    log.info(f"   房间窗 hwnd={hwnd}：开始游戏={sc:.3f}")
                    return "ready" if sc >= th else "busy"
                return "unknown"
            except Exception:
                return "unknown"
        try:
            r = rf.find_room(verbose=False, restore=False)
            if r:
                sc = float(r.get("start_score") or 0)
                log.info(f"   房间窗 hwnd={r['hwnd']}：开始游戏={sc:.3f}")
                return "ready" if sc >= th else "busy"
        except Exception:
            return "unknown"
        # 没绑定、也没认出房间：再看看还有没有「房间体型」的平台窗（1224x904 那种）。
        # 一扇都没有才能判「房间没了」—— 详情页(1332x945)/建房弹窗(584x517) 不算。
        try:
            cand = [w for w in rf.platform_windows(min_client=300)
                    if 1100 <= w["client"][2] <= 1350 and 820 <= w["client"][3] <= 980]
            if not cand:
                log.info("   平台只剩主窗/弹窗，没有房间体型的窗口")
                return "gone"
        except Exception:
            return "unknown"
        return "unknown"

    def _room_start_ready(self):
        """兼容旧调用：True / False / None。"""
        st = self._room_state()
        return {"ready": True, "busy": False, "gone": False}.get(st)

    def _disc_guard_once(self, where: str = "等按钮") -> bool:
        """等大厅期间收掉「断线重连 / 断线重连失败」弹窗，然后决定要不要重开一局。

        2026-11-27 16:20 用户定（**覆盖**之前「命中就重启 KK 平台」的做法）：
          ① 命中「断线重连」→ **先按 ESC**；ESC 关不掉才点「取消」
             （绝不点「断线重连(30)」—— 那会去连一局已经死掉的游戏）
          ② 紧随其后可能弹「断线重连失败。」→ 同样 **先 ESC**；不行点「确定」
          ③ 弹窗收掉后看房间窗：
             · 「开始游戏」**亮着** → 抛 GameGone，交回 pipeline 回 ④ 重新点开始游戏
               （房间还在，重开最省时，不重启平台）
             · 没亮（平台仍认为在游戏中）→ 不重启、不重复点，继续等大厅
          全程不关闭窗口、不杀进程。
        """
        dg = getattr(self, "_dg", None)
        if dg is None:
            try:
                import disc_guard as _dg
            except Exception:
                here = os.path.dirname(os.path.abspath(__file__))
                if here not in sys.path:
                    sys.path.insert(0, here)
                try:
                    import disc_guard as _dg
                except Exception as e:
                    log.warning(f"   (断线重连守卫不可用：{e!r})")
                    return False
            dg = _dg
            self._dg = dg
        try:
            hit, kind = dg.find(verbose=False), "断线重连"
            if hit is None:
                hit, kind = dg.find_fail(verbose=False), "断线重连失败"
        except Exception:
            return False
        if hit is None:
            return False
        log.warning("=" * 56)
        log.warning(f"⛔ {where}期间出现「{kind}」弹窗"
                    f"（{hit['w']}x{hit['h']} score={hit['score']:.3f}）"
                    " → 先 ESC，关不掉再点「取消」")
        r1 = r2 = "none"
        try:
            r1 = dg.dismiss(verbose=True)          # ESC 优先 → 关不掉才点「取消」
        except Exception as e:
            log.warning(f"   ⚠ 关「断线重连」出错：{e!r}")
        try:
            r2 = dg.dismiss_fail(verbose=True)     # 「断线重连失败。」→ ESC 优先 → 确定
        except Exception as e:
            log.warning(f"   ⚠ 关「断线重连失败」出错：{e!r}")
        log.warning(f"   弹窗处理：断线重连={r1}  重连失败={r2}")
        still = False
        try:
            still = bool(dg.find(verbose=False) or dg.find_fail(verbose=False))
        except Exception:
            still = False
        if still:
            log.warning("   ⚠ 弹窗还挂着（ESC/取消都没关掉）→ 不重启平台，下轮再试")
            return True
        radio = self._room_state()
        if radio == "ready":
            log.warning("   房间「开始游戏」亮着 → 回 ④ 重新点开始游戏（不重启平台）")
        elif radio == "gone":
            log.warning("   房间窗已经没了 → 放弃 ④，重新 ①②③ 搜图建房")
        elif radio == "busy":
            log.warning("   房间「开始游戏」未亮（平台仍认为在游戏中）→ 继续等大厅，不重开")
        else:
            log.warning("   房间状态判不了 → 继续等大厅，不重开（保守）")
        log.warning("=" * 56)
        try:
            dg._log_event("disconnect_recover", where=where, kind=kind,
                          result=f"{r1}/{r2}", detail={"room_state": radio})
        except Exception:
            pass
        if radio == "ready":
            raise GameGone(f"{where}期间出现「{kind}」→ 已用 ESC/取消收掉，"
                           f"房间「开始游戏」亮着 → 回 ④ 重新开局")
        if radio == "gone":
            raise RoomGone(f"{where}期间出现「{kind}」→ 已用 ESC/取消收掉，"
                           f"房间窗已不存在 → 重新搜图建房")
        return True

    def _wait_image_loop(self, spec):
        """等图入口：支持 quiet_focus_s（前 N 秒不抢前台）

        2026-11-26 用户选 B：点完「开始游戏」后 War3 要启动并载入地图，这段时间
        原来每 3 秒一次 focus()（日志里一条条「游戏窗口不在前台，自动切回」）会反复
        把窗口抢到前台。W7 上 War3 起来 18 秒就没了，怀疑就是这段打断。
        这里给「等大厅」这一步一个可配置静默期：期内 _checkpoint 完全不碰窗口焦点。
        （_bind_by_content 保留：它只抓图、不点击、不抢前台，而且 War3 重建窗口后
          要靠它按画面接上新窗口。）
        """
        quiet = float(spec.get("quiet_focus_s", 0) or 0)
        if quiet <= 0:
            return self._wait_image_loop_inner(spec)
        self._quiet_focus_until = time.time() + quiet
        log.info(f"   （前 {quiet:.0f}s 不抢前台：War3 正在启动/载入）")
        try:
            return self._wait_image_loop_inner(spec)
        finally:
            self._quiet_focus_until = 0.0

    def _wait_image_loop_inner(self, spec):
        """等一张图出现；spec["any_window"]=True 时允许「窗口换了也算中」"""
        template = spec["template"]
        timeout = float(spec.get("timeout", 8000))
        th = float(spec.get("threshold", 0.85))
        interval = float(spec.get("interval", 200))
        anyw = bool(spec.get("any_window"))
        t0 = time.time()
        # 进度心跳（2026-11-18 18:01 教训）：换电脑后这一步可能静默等几十秒，
        # 用户看不到任何输出就当成卡死手动停掉（其实 War3 正在载入地图）。
        # 每 30 秒报一次「在等什么 / 已等多久 / 当前窗口多大」，一眼能看出是活着还是死了。
        beat = 0.0
        last_scan = 0.0
        # 抓不到画面（0x0 / 抓图失败）时的重抓节流（用户 2026-11-18 20:55 定）：
        # **不提前退出**，一直耗到 timeout 上限；抓不到就每 30 秒重抓一次，别一秒一次空抓。
        # any_window 的保活扫描是另一条路（30 秒一次，2026-11-19 17:14 定 15 秒、17:44 改 30）：
        # 窗口真消失时由 _checkpoint 用 2 秒节流立刻按画面重绑，不靠这个慢轮询；
        # 这里只是巡检「旧窗口还活着但画面没了」这种半死不活的状态。
        dead_next = 0.0
        dead_noted = False
        # 断线重连顺手查（用户 2026-11-18 22:50 要求）：等满 3 秒先查一次，之后每 30 秒一次。
        # 为什么要等 3 秒：一次检查要枚举窗口 + 抓图，约 0.5 秒，正常 30~60 秒就命中按钮的
        # 情况不该被它拖慢（第一版从第 0 秒就查，等图节奏被拖到 5 次/2 秒，自检 ② 抓到）。
        # 只点弹窗按钮，不退出房间、不发任何关闭/杀进程指令。
        disc_next, disc_n = 3.0, 0
        disc_on = bool(spec.get("disc_check", True))
        # 2026-11-27 16:20 用户定：等大厅期间每 6 秒巡检一次「断线重连」弹窗
        # （弹窗按钮上的倒计时也是 30 秒，30 秒查一次经常来不及）。
        _disc_gap = float(spec.get("disc_interval_s")
                          or (getattr(self, "cfg", None) or {}).get("disc_interval_s")
                          or 6)
        # 进程看门狗（用户 2026-11-18 23:26 实测）：他在任务管理器里直接结束 War3，
        # 平台**没有**弹「断线重连」，而是直接回房间 → 上面那条「关掉弹窗后才查进程」
        # 根本走不到，程序只能对着 0x0 的窗口干等 900 秒。
        # 所以这里独立查进程：本次等待里**见过 War3 活着**之后，连续 2 次（30 秒一次）
        # 都查不到才判「这一局结束」。两个前提都不能省：
        #   · 必须先见过活着 —— 点开始游戏后 War3 要十几秒才起来，一开始没有很正常；
        #   · 不能只看窗口 —— 载入地图时窗口是 0x0 但进程还在（17:04 那台等了 900s 最后正常进游戏）。
        proc_next, seen_alive, gone_times = 3.0, False, 0
        # 静止降频（2026-11-19 降 CPU）：等大厅 / 等载入时画面几十秒一动不动，
        # 但原来照样按 interval 高频「抓图 + 多档匹配」。实测单帧约 300ms（3 档），
        # 比 interval 还长 → 循环背靠背连续跑，一个核直接吃满。
        # 这里用 64x36 缩略图（约 2.7ms）判断画面动没动：连续 3 次几乎没变就
        # 把间隔放宽到 2.5 倍（上限 600ms）；画面一变立刻回到原节奏。
        # 只影响「没命中」的分支，命中照旧立刻返回，不会拖慢识别。
        still_n = 0
        # 上限 3000ms 只是防呆：等大厅按钮那一步有 12 档缩放（每档约 96ms），
        # 单帧就 1.15 秒，而 interval 只有 1000ms —— 画面静止时放到 2.5 倍才真省得下来。
        slow_max_ms = 3000.0
        prev_thumb = None
        # ⚠️ 默认关闭：别的等图步骤（等平台按钮）本来就没有 War3 进程，
        # 跟着查会误判。只在大厅等按钮那一步由配置显式打开。
        proc_on = bool(spec.get("proc_watch", False))
        spawn_limit = float(spec.get("spawn_wait_s", 150))
        while True:
            self._checkpoint()
            now = time.time()
            if dead_next > now:
                img = None          # 还没到 30 秒，这次不去抓那个空窗口
            else:
                try:
                    img = self.cap.grab(self.win)
                except Exception:
                    img = None      # 窗口没了/被最小化 → 当没命中，交给下面的 any_window
                if img is None or getattr(img, "size", 0) == 0:
                    img = None
                    dead_next = now + 30.0
                    if not dead_noted:
                        dead_noted = True
                        log.warning(f"   …抓不到画面（窗口 0x0 / 已消失）→ 每 30 秒重抓一次，"
                                    f"一直等到 {timeout / 1000:.0f}s 上限（不提前退出）")
                else:
                    dead_noted = False
                    try:
                        thumb = cv2.resize(img, (64, 36),
                                           interpolation=cv2.INTER_AREA)
                    except Exception:
                        thumb = None
                    if thumb is not None and prev_thumb is not None \
                            and thumb.shape == prev_thumb.shape:
                        if float(cv2.absdiff(thumb, prev_thumb).mean()) < 0.8:
                            still_n += 1
                        else:
                            still_n = 0      # 画面动了 → 立刻恢复正常节奏
                    prev_thumb = thumb
            if img is None:
                name, hit = None, None
                blank = True        # 抓不到画面 = 这轮没法确认，按「空窗」算
            else:
                name, hit = self.vis.find_best(template, img, th, spec.get("region"),
                                               self._scales_with_ref(spec))
                # 画面抓得到、但几乎是纯色（全黑 / 空窗口残留）→ 也当「空窗」。
                # War3 重建窗口时旧句柄可能还活着、PrintWindow 抓出来一片黑，
                # 靠这个判据才能 2 秒内切到新窗口，而不是等 30 秒巡检。
                blank = thumb is not None and float(thumb.std()) < 3.0
            if hit:
                log.info(f"图像命中 {name} score={hit[4]:.3f} @({hit[0]},{hit[1]})")
                return hit
            el = time.time() - t0
            if disc_on and el >= disc_next:
                disc_next = el + _disc_gap
                handled = False
                try:
                    handled = self._disc_guard_once()
                except GameGone:
                    raise                       # 弹窗已收掉且房间可开局 → 回 ④ 重开
                except PlatformRestarted:
                    raise
                except PlatformLoginRequired:
                    raise
                except Exception as e:
                    log.warning(f"   (断线重连检查跳过：{e!r})")
                    disc_next = el + 120.0
                if handled:
                    disc_n += 1
                    # 2026-11-27 16:20 用户定：弹窗收掉了但房间还不能开局 → 不重启平台、
                    # 不重复点「开始游戏」，继续等大厅（能重开时上面已经抛 GameGone 了）。
                    log.info(f"   ↩ 断线弹窗已收掉（第 {disc_n} 次）→ 继续等大厅")
            if proc_on and el >= proc_next:
                proc_next = el + 30.0
                try:
                    _alive = war3_alive()
                except Exception:
                    _alive = True           # 判不了就当还活着（保守：继续等，绝不乱重开）
                if _alive:
                    if not seen_alive:
                        log.info("   …War3 进程在，继续等大厅")
                    seen_alive, gone_times = True, 0
                elif seen_alive:
                    gone_times += 1
                    log.warning(f"   ⚠ War3 进程已不在（第 {gone_times}/2 次检查）"
                                f"—— 窗口 0x0 其实不用再等了")
                    if gone_times >= 2:
                        raise GameGone("等大厅期间 War3 进程消失，平台也没弹「断线重连」"
                                       "（这一局已结束）→ 回房间重新点「开始游戏」")
                elif el >= spawn_limit:
                    # 一次都没见过 War3 进程：④ 点过「开始游戏」却连进程都没起来，
                    # 典型是地图版本不一致导致的秒退。等下去也是白等。
                    raise GameGone(f"点了「开始游戏」后 {spawn_limit:.0f}s 仍没有 War3 进程"
                                   f"（大概率秒退）→ 回房间重新点「开始游戏」")
            # any_window：当前窗口一直不中，就看看别的窗口是不是已经画出来了。
            # 为什么需要：War3 可能把新窗口叠在旧窗口旁边、旧句柄还活着但画面是空的，
            # 这时光盯一个句柄永远等不到（2026-11-18 18:43 用户提的思路）。
            # 2026-11-19 17:14 用户定 1.0s → 15.0s，17:44 改 30.0s（单次扫描抓图 100ms
            # + 匹配 41ms ≈ 167ms，2 秒一次等于常驻吃掉 8.4% 单核，30 秒一次约 0.6%；
            # War3 重建窗口本来就要好几秒，等大厅通常几十秒，30 秒巡检足够）。
            # 例外（回 2 秒一次）：当前窗口**没法确认画面**时 ——
            #   · 抓都抓不到（img is None / 0x0，用例③）
            #   · 抓到了但几乎是纯色（黑屏 / 空窗口残留，用例④）
            # 这一刻「找别的窗口」是唯一有意义的动作，也没有别的抓图开销。
            # 只有「窗口确实画着东西、只是不是大厅」时才走 30 秒巡检 ——
            # 那种情况扫别的窗口也没用，新窗口真出来了画面不可能还是原来那样。
            _scan_gap = 2.0 if blank else 30.0
            if anyw and el - last_scan >= _scan_gap:
                last_scan = el
                if self._bind_by_content(spec):
                    # 换了窗口（War3 重建出来了）→ 30 秒节流立刻作废，正常 200ms 抓，
                    # 否则会一直卡在「不抓新窗口」上，白等 30 秒（自检 ③ 踩到过）。
                    dead_next = 0.0
                    dead_noted = False
                    log.info("   …这张图出现在另一个窗口上 → 已切换过去")
                    self._sleep(interval)
                    continue
            if el - beat >= 30:
                beat = el
                try:
                    _l, _t, _cw, _ch = self.win.client_rect()
                    _size = f"{_cw}x{_ch}"
                except Exception:
                    _size = "?"
                log.info(f"   …还在等 {template}（已等 {el:.0f}s / "
                         f"最长 {timeout / 1000:.0f}s，窗口 {_size}）")
            if el * 1000 > timeout:
                # 存现场图：换电脑后「等不到按钮」十有八九是尺寸/模板失配，
                # 没有截图只能靠猜（2026-11-18 17:04 就是这么白猜了一轮）
                self._dump_miss(template)
                if spec.get("required", False):
                    raise RuntimeError(f"等待超时：{template}")
                log.warning(f"等待超时：{template}")
                return None
            _sleep_ms = interval
            if still_n >= 3:
                _sleep_ms = max(interval, min(slow_max_ms, interval * 2.5))
                if still_n == 3:
                    log.info(f"   …画面静止 → 放慢到 {_sleep_ms:.0f}ms 一轮省 CPU"
                             f"（画面一变立刻恢复）")
            self._sleep(_sleep_ms)

    def _scales_with_ref(self, spec) -> list:
        """把配置里的 scales 与「按当前窗口/基准窗口算出的 (fx, fy)」合并

        模板都是在基准窗口（大厅 1690x921）下截的。换电脑后 War3 的客户区可能是
        1264x921、1200x830 这种「横向被压扁」的尺寸：等比缩放对不上，真正对得上的是
        fx=宽/1690、fy=高/921 这一档。钓鱼模式当初是「先 resize 到 1690x921 再匹配」
        解决的同一个问题，这里用等价做法（按两个方向分别缩放模板）。
        """
        try:
            _, _, cw, ch = self.win.client_rect()
        except Exception:
            cw = ch = 0
        return self._scales_for(spec, cw, ch)

    def _dump_miss(self, tpls):
        """找图失败时把当时画面存下来，方便事后调模板"""
        try:
            name = tpls if isinstance(tpls, str) else "-".join(tpls)
            tag = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", os.path.basename(name))[:40]
            d = os.path.join(os.path.dirname(self.cfg_dir) or self.cfg_dir,
                             "logs", "miss")
            os.makedirs(d, exist_ok=True)
            out = os.path.join(d, datetime.now().strftime(f"miss_{tag}_%H%M%S.png"))
            ok, buf = cv2.imencode(".png", self.cap.grab(self.win))
            if ok:
                buf.tofile(out)
                log.warning(f"已保存失败现场截图: {out}")
        except Exception:
            pass

    def run_task(self, name: str):
        tasks = self.cfg.get("tasks", {})
        if name not in tasks:
            raise KeyError(f"配置里没有任务 '{name}'，可用: {list(tasks)}")
        task = tasks[name]
        steps = task["steps"] if isinstance(task, dict) else task
        repeat = task.get("repeat", 1) if isinstance(task, dict) else 1
        # 任务级 step_gap 覆盖全局：每个「按键/点击/输入」动作之后自动停这么久
        if isinstance(task, dict) and task.get("step_gap") is not None:
            self._step_gap = float(task["step_gap"] or 0)
        if self._step_gap:
            log.info(f"   动作间隔 step_gap = {int(self._step_gap)} ms")
        self._current_task = name
        log.info(f"▶ 开始任务 [{name}]  repeat={repeat}  steps={len(steps)}")
        # 任务级「锚点图」：整个任务期间都拿它按画面找窗口。
        # 大厅这步（wuxia_lobby）配上它以后，中间任何一步发现窗口没了，都会先找
        # 「谁画出了这张图」再接上去，不用管句柄换没换（2026-11-18 18:49 用户要求）。
        prev_spec = getattr(self, "_wait_spec", None)
        if isinstance(task, dict) and task.get("anchor_template"):
            self._wait_spec = {"template": task["anchor_template"],
                               "threshold": float(task.get("anchor_threshold", 0.85)),
                               "any_window": True}
            log.info(f"   窗口锚点：按画面内容找（{task['anchor_template']}）")
        try:
            if not self.win.focus():
                log.warning("窗口切前台失败，仍继续（请确保游戏窗口在前台）")
            i = 0
            while repeat == 0 or i < repeat:
                i += 1
                if repeat != 1:
                    log.info(f"--- 第 {i} 轮 ---")
                self.exec_steps(steps)
        finally:
            self._wait_spec = prev_spec
            self.input.release_all()
            log.info(f"■ 任务 [{name}] 结束")

    def exec_steps(self, steps):
        for step in steps:
            self._checkpoint()
            self.exec_step(step)
            # step_gap：每个动作步骤后统一停一下，避免连按太快被游戏吞掉
            if self._step_gap and isinstance(step, dict):
                kind = None
                for k in step:
                    if k.lower() in STEP_KINDS:
                        kind = k.lower()
                        break
                if kind in ACTION_KINDS:
                    self._sleep(self._step_gap)

    def exec_step(self, step: dict):
        if not isinstance(step, dict):
            raise ValueError(f"步骤格式错误: {step!r}")
        # 取第一个「已知步骤类型」的键，避免 gap/threshold 这类修饰键写在前面时误判
        kind, arg = None, None
        for k, v in step.items():
            if k.lower() in STEP_KINDS:
                kind, arg = k.lower(), v
                break
        if kind is None:
            raise ValueError(f"无法识别的步骤: {list(step)}")

        # -- 等待 --
        if kind in ("wait", "sleep"):
            self._sleep(float(arg))
            return

        if kind == "log":
            log.info(str(arg))
            return

        if kind == "check_ad":
            # 点之前清一遍平台上的广告浮层/活动弹窗。
            # 用户 2026-11-17 20:58：房间页随机弹「216U币 红包」，它是模态的 ——
            # 弹着的时候点「选难度」「开始游戏」都没反应（视觉上并不遮挡），
            # 只能先点右上 X 关掉再点。配置里写成 {"check_ad": {}} 即可。
            spec = arg if isinstance(arg, dict) else {}
            if getattr(self, "dry_run", False):
                log.info("   [dry] 跳过清广告（干跑不点真实平台窗口）")
                return
            acted = _close_platform_popups(verbose=bool(spec.get("verbose", False)))
            if acted:
                log.info(f"   🧹 已清掉挡在前面的{ '、'.join(acted) }")
            return

        if kind == "stop":
            raise StopBot()

        if kind == "focus":
            self.win.focus()
            return

        # -- 键盘 --
        if kind == "key":
            # hold：按住多久。默认 40ms 在 60fps 下够用，但战况激烈掉到 5~10fps 时
            # （一帧 100~200ms）War3 按帧轮询键盘，40ms 可能整个落在两帧之间被完全丢掉，
            # 所以需要「确实要被游戏看到的键」把它显式设大（150~250ms）。
            hold = float(step.get("hold", 40)) / 1000.0
            presses = int(step.get("presses", 1))
            self.input.key(str(arg), hold=hold, presses=presses)
            return
        if kind == "esc_safe":
            # 2026-11-26：按 ESC 清场前先排除「地图已更新，请重新下载地图」提示。
            # ESC 会把那个提示关掉 → 平台不下载新版地图 → 开局 War3 秒退（用户反馈的「地图更新失效」）。
            # 有提示就不按，留给 ④ 开局前的 map_update 守卫去「点确定 + 重建房间」。
            spec = arg if isinstance(arg, dict) else {}
            times = max(1, int(spec.get("times", 1)))
            gap = float(spec.get("gap", 700))
            try:
                import map_update as _mu
                if _mu.has_update_window() and _mu.find(verbose=False):
                    log.info("   ⏭ 屏幕上有「地图已更新」提示 → 跳过 ESC"
                             "（按了会把提示关掉、地图不下载）")
                    return
                # 断线重连 / 断线重连失败：用户 2026-11-26 实测 **按 ESC 就能关掉**，
                # 所以这里不拦，正常按 ESC 处理。
            except Exception:
                pass
            # 2026-11-26 22:37 实测（房间窗保护）：正常退出游戏后平台回到的是**房间窗**
            # （1224x904），紧接着「局间 ESC ×2」绑到的就是它 —— 房间窗上的 ESC = 退出房间
            # （第 1 次弹「是否确认退出房间？」，5 秒后第 2 次确认），8 秒后房间窗就没了，
            # 下一轮判「没有房间」→ 搜地图建房 → 房间⇄详情页死循环。
            # ESC 前先认一下：当前窗口是房间窗就别按，房间保住，下一轮 ①②③ 直接跳过。
            try:
                import room_finder as _rf_esc
                if _rf_esc.is_room_window(getattr(self.win, "hwnd", 0)):
                    log.info("   ⏭ 当前窗口是房间窗（ESC 等于退出房间）→ 跳过 ESC，保留房间")
                    return
            except Exception:
                pass
            for i in range(times):
                self._checkpoint()
                self.input.key("esc", hold=0.06)
                if i + 1 < times:
                    self._sleep(gap)
            return
        if kind in ("keys", "combo"):
            gap = float(step.get("gap", 80))
            for k in arg:
                self._checkpoint()
                self.input.key(str(k))
                self._sleep(gap)
            return
        if kind == "key_until":
            # 反复按同一个键，直到画面上出现指定模板为止。
            # 两个实测坑：
            #   1) 菜单刚弹出时有「输入锁定期」，早按会被吞（1.4s 无效 / 5.5s 有效，不稳）
            #   2) 战况激烈时帧率低，菜单面板渲染慢，按完固定等 1.2s 常常还没画出来
            # 所以策略是：按一次 → 之后每 300ms 探一次、最多等 wait_ms → 没出来再按，
            # 一旦画面出现立刻返回，既不会早按浪费，也不会被卡顿坑。
            spec = arg if isinstance(arg, dict) else {"key": str(arg)}
            key = str(spec["key"])
            tpl = spec["template"]
            th = float(spec.get("threshold", 0.88))
            tries = int(spec.get("tries", 3))
            wait_ms = float(spec.get("wait_ms", 3000))
            interval = float(spec.get("interval", 300))
            initial = float(spec.get("initial", 0))
            settle = float(spec.get("settle", 400))
            # 按住时长：低帧率下 40ms 的按键会被整帧漏掉（见上面 key 的说明），这里默认 150ms
            hold = float(spec.get("hold", 150)) / 1000.0

            def _hit():
                _, h = self.vis.find_best(tpl, self.cap.grab(self.win), th,
                                          spec.get("region"), self._scales_with_ref(spec))
                return h

            if initial > 0:
                self._sleep(initial)
            for i in range(tries + 1):          # +1：第一次先白探一次（菜单可能已经开着）
                self._checkpoint()
                if _hit():
                    log.info(f"   「{tpl}」已在画面上（按 {key.upper()} {i} 次后）")
                    return True
                if i >= tries:
                    break
                # 只在窗口确实不在前台时才抢焦点：反复 SetForegroundWindow 会让游戏
                # 反复进出「激活」状态，输入反而更容易被吞
                if not self.win.fg_same_process():
                    log.info(f"   窗口不在前台 → 先切回来再按 {key.upper()}")
                    self.win.focus()
                    self._sleep(settle)
                self.input.key(key, hold=hold)
                # 按完高频探：面板一画出来就立刻往下走
                t0 = time.time()
                while (time.time() - t0) * 1000 < wait_ms:
                    self._checkpoint()
                    if _hit():
                        log.info(f"   「{tpl}」已在画面上（按 {key.upper()} {i + 1} 次后）")
                        return True
                    self._sleep(interval)
            if spec.get("required", False):
                raise RuntimeError(f"按 {key.upper()} 试了 {tries} 次，{tpl} 始终没出现")
            # 留证据：这时画面是什么样、模板最高分多少（低帧率/面板没开/模板失配都能看出来）
            try:
                img = self.cap.grab(self.win)
                low = self.vis.find(tpl, img, 0.0, spec.get("region"), spec.get("scales"))
                best = None if low is None else low[4]
                d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "miss")
                os.makedirs(d, exist_ok=True)
                fp = os.path.join(d, f"key_until_{key}_{time.strftime('%H%M%S')}.png")
                ok, buf = cv2.imencode(".png", img)
                if ok:
                    buf.tofile(fp)          # imencode+tofile：cv2.imwrite 遇中文路径会静默失败
                log.warning(f"   ⚠️ 按 {key.upper()} 试了 {tries} 次，{tpl} 仍没出现"
                            f"（目前最高分 {best if best is None else round(best, 3)}，"
                            f"现场图 logs/miss/{os.path.basename(fp)}）")
            except Exception as e:
                log.warning(f"   ⚠️ 按 {key.upper()} 试了 {tries} 次，{tpl} 仍没出现（存图失败：{e}）")
            return False
        if kind == "hotkey":
            parts = [p.strip() for p in str(arg).split("+") if p.strip()]
            for p in parts[:-1]:
                self.input.key_down(p)
            self.input.key(parts[-1])
            for p in reversed(parts[:-1]):
                self.input.key_up(p)
            return
        if kind == "hold":
            self.input.key(str(arg), hold=float(step.get("ms", 200)) / 1000.0)
            return
        if kind == "keydown":
            self.input.key_down(str(arg))
            return
        if kind == "keyup":
            self.input.key_up(str(arg))
            return
        if kind == "repeat_key":
            k = str(arg)
            self.input.key(k, hold=float(step.get("ms", 40)) / 1000.0,
                           presses=int(step.get("times", 10)),
                           gap=float(step.get("gap", 60)) / 1000.0)
            return

        # -- 鼠标 --
        if kind == "move":
            x, y = arg
            self.input.move(int(x), int(y))
            return
        if kind == "click":
            if isinstance(arg, dict):
                self.input.click(int(arg["x"]), int(arg["y"]),
                                 arg.get("button", "left"),
                                 float(arg.get("hold", 50)) / 1000.0,
                                 int(arg.get("clicks", 1)))
            else:
                self.input.click(int(arg[0]), int(arg[1]))
            return
        if kind == "drag":
            self.input.drag(*[int(v) for v in arg])
            return

        # -- 图像/像素条件 --
        if kind == "wait_image":
            spec = arg if isinstance(arg, dict) else {"template": arg}
            prev = getattr(self, "_wait_spec", None)
            self._wait_spec = spec      # 窗口中途失效时，_checkpoint 靠它按画面找回来
            try:
                return self._wait_image_loop(spec)
            finally:
                self._wait_spec = prev

        if kind == "if_image":
            spec = arg if isinstance(arg, dict) else {"template": arg}
            _, hit = self.vis.find_best(spec["template"], self.cap.grab(self.win),
                                        float(spec.get("threshold", 0.85)),
                                        spec.get("region"), self._scales_with_ref(spec))
            # then/else 既可以写在步骤外层，也可以写在参数内层
            then_b = step.get("then", spec.get("then", []))
            else_b = step.get("else", spec.get("else", []))
            branch = then_b if hit else else_b
            # 未命中且没写 else 就不打日志（看门狗这类高频轮询会把日志刷爆）
            if hit or branch:
                log.info(f"条件图像 {spec['template']} → {'then' if hit else 'else'}"
                         f"（{'命中' if hit else '未命中'}，{len(branch)} 步）")
            if branch:
                self.exec_steps(branch)
            return

        if kind == "wait_pixel":
            x, y = arg
            color = parse_color(step.get("color"))
            tol = int(step.get("tol", 25))
            timeout = float(step.get("timeout", 8000))
            t0 = time.time()
            while True:
                self._checkpoint()
                img = self.cap.grab(self.win)
                b, g, r = img[int(y), int(x)][:3]
                if (abs(int(b) - color[0]) <= tol and abs(int(g) - color[1]) <= tol
                        and abs(int(r) - color[2]) <= tol):
                    return True
                if (time.time() - t0) * 1000 > timeout:
                    if step.get("required", False):
                        raise RuntimeError(f"像素等待超时 {x},{y}")
                    return False
                self._sleep(float(step.get("interval", 150)))

        if kind == "if_pixel":
            x, y = arg
            img = self.cap.grab(self.win)
            b, g, r = img[int(y), int(x)][:3]
            color = parse_color(step.get("color"))
            tol = int(step.get("tol", 25))
            ok = (abs(int(b) - color[0]) <= tol and abs(int(g) - color[1]) <= tol
                  and abs(int(r) - color[2]) <= tol)
            branch = step.get("then", []) if ok else step.get("else", [])
            if branch:
                self.exec_steps(branch)
            return

        if kind == "click_image":
            spec = arg if isinstance(arg, dict) else {"template": arg}
            tpls = spec["template"]
            th = float(spec.get("threshold", 0.85))
            timeout = float(spec.get("timeout", 5000))
            scales = self._scales_with_ref(spec)
            clicks = int(spec.get("clicks", 1))
            interval = float(spec.get("interval", 200))
            t0 = time.time()
            while True:
                self._checkpoint()
                name, hit = self.vis.find_best(tpls, self.cap.grab(self.win), th,
                                               spec.get("region"), scales)
                if hit:
                    dx, dy = spec.get("offset", [0, 0])
                    cx = hit[0] + hit[2] // 2 + int(dx)
                    cy = hit[1] + hit[3] // 2 + int(dy)
                    log.info(f"命中 {name} score={hit[4]:.3f} @({hit[0]},{hit[1]}) "
                             f"→ {'双击' if clicks == 2 else '单击'} ({cx},{cy})")
                    # 2026-11-26 23:50：点「创建房间」前先确认这个坐标最上层是自己。
                    # 退出游戏后房间窗（1224x904）常压在主窗上：按主窗坐标点下去会被它吃掉，
                    # 建房弹窗永远不出来，连点 3 次后整条链路重跑 → 房间⇄详情页死循环。
                    # 被平台窗口盖住 → 不点（不盲打），记下它让 _open_create_dialog 判「房间还在」。
                    if "btn_create_room" in str(tpls):
                        try:
                            import room_finder as _rf_top
                            _top = _rf_top.top_window_at(self.win.hwnd, cx, cy)
                            if _top and _top != self.win.hwnd:
                                _w = next((x for x in _rf_top.platform_windows(min_client=300)
                                           if x["hwnd"] == _top), None)
                                if _w:
                                    _cw, _ch = _w["client"][2], _w["client"][3]
                                    # 只拦「房间窗/详情页」这个尺寸段（1150~1300 × 850~960）。
                                    # 584x517 建房弹窗、广告浮层不算 —— 认错了会把正常点击也拦掉。
                                    if 1100 <= _cw <= 1350 and 820 <= _ch <= 980:
                                        ROOM_BLOCKED["hwnd"] = _top
                                        log.warning(f"   ⚠ 「创建房间」点击位({cx},{cy})被平台窗口 "
                                                    f"hwnd={_top} {_cw}x{_ch} 盖住 → 不点（避免盲打）")
                                        return None
                                    log.warning(f"   ⚠ 「创建房间」点击位被 {_cw}x{_ch} 的平台窗盖住"
                                                f"（不是房间窗尺寸）→ 照常点")
                        except Exception as _e:
                            log.info(f"   (点击位最上层校验出错，照常点：{type(_e).__name__})")
                    self.input.click(cx, cy, spec.get("button", "left"),
                                     float(spec.get("hold", 50)) / 1000.0, clicks,
                                     gap=float(spec.get("click_gap", 50)) / 1000.0)
                    return hit
                if (time.time() - t0) * 1000 > timeout:
                    self._dump_miss(tpls)
                    fb = spec.get("fallback")
                    if fb:
                        # fallback 坐标也是按参考窗口写死的，换窗口大小要等比换算，
                        # 否则会把鼠标点到旁边去（用户 2026-11-17 换个电脑就踩到这个）
                        fsx = fsy = 1.0
                        fbr = spec.get("fallback_ref")
                        if fbr:
                            try:
                                _im = self.cap.grab(self.win)
                                fsx = _im.shape[1] / float(fbr[0])
                                fsy = _im.shape[0] / float(fbr[1])
                            except Exception:
                                fsx = fsy = 1.0
                        fbx, fby = int(round(fb[0] * fsx)), int(round(fb[1] * fsy))
                        log.warning(f"没找到 {tpls}，改用固定坐标 ({fbx},{fby})"
                                    + (f"（按 {fsx:.2f}x{fsy:.2f} 换算）" if fbr else ""))
                        self.input.click(fbx, fby,
                                         spec.get("button", "left"),
                                         float(spec.get("hold", 50)) / 1000.0, clicks)
                        return None
                    if spec.get("required", False):
                        raise RuntimeError(f"找不到可点击目标：{tpls}")
                    log.warning(f"未找到：{tpls}")
                    return None
                self._sleep(interval)

        # -- 点「列表里最下面那一行」：用于难度 N1..N20 里选最高难度 --
        # 老地图一屏 7 行且不可滚动，直接取最底行即可；新版本若列表变长/可滚动，
        # 配 "scroll": N 会先滚到底（行签名不再变化即视为到底）再取最底行。
        if kind in ("click_last_row", "click_max_row", "click_row", "click_nth_row"):
            spec = arg if isinstance(arg, dict) else {}
            clicks = int(spec.get("clicks", 1))
            kw = dict(bright=int(spec.get("bright", 26)),
                      min_h=int(spec.get("min_h", 20)),
                      max_h=int(spec.get("max_h", 70)),
                      min_w=int(spec.get("min_w", 80)))
            min_rows = int(spec.get("min_rows", 3))
            retries = max(1, int(spec.get("retries", 4)))
            retry_wait = float(spec.get("retry_wait", 600)) / 1000.0
            times = int(spec.get("scroll", 0) or 0)

            # 目标行号：spec["row"] = 1 基行号 / "auto"（读 logs/diff_target.json）/ 缺省=最下面一行
            want, want_name, tag = spec.get("row", None), spec.get("name"), "lobby"
            if want is None or (isinstance(want, str) and want.lower() == "auto"):
                r_, nm, tg = read_diff_target()
                if r_ is None:
                    log.info("没读到本轮目标难度（logs/diff_target.json）→ 取最下面一行（最高）")
                    want = None
                else:
                    want, want_name, tag = r_, nm or want_name, tg or "lobby"
                    log.info(f"本轮目标难度：{want_name or '?'} → 第 {want} 行"
                             f"（来自 logs/diff_target.json）")
            else:
                want = int(want)
                log.info(f"目标难度：{want_name or '?'} → 第 {want} 行（配置写死）")

            # 大厅刚出现时难度列表是「一行一行画出来」的：17:23 实测，按钮 0.971 已命中、
            # 点下去前 2 秒里列表只画出 1 行，等 2.4s 就放弃 → 一个难度都没选就开打了。
            # 所以改成**按总时长等**（wait_rows，默认 15s），列表画够了才点。
            wait_rows = float(spec.get("wait_rows", 15))
            deadline = time.time() + max(wait_rows, retries * retry_wait)
            # 参考客户区尺寸：配置里的 region / 行高阈值 / fallback 行距都是按这个尺寸调好的
            ref = spec.get("ref")
            reg_spec = spec.get("region")
            rows, img, region, attempt = [], None, None, 0
            prev_len = 0
            sx = sy = 1.0
            fitted = False
            while True:
                attempt += 1
                img = self.cap.grab(self.win)
                if not fitted:
                    kw, reg_spec, sx, sy = fit_spec(kw, reg_spec, img, ref)
                    fitted = True
                    if ref:
                        log.info(f"难度列表：当前窗口 {img.shape[1]}x{img.shape[0]}，"
                                 f"按参考 {int(float(ref[0]))}x{int(float(ref[1]))} 换算 "
                                 f"{sx:.2f}x{sy:.2f} 倍 → region={reg_spec}"
                                 f" 行高 {kw['min_h']}~{kw['max_h']}")
                region = reg_spec or [0, 0, img.shape[1], img.shape[0]]
                rows = detect_rows(img, region, **kw)
                # 高亮行的边框会画得更大，和下一行粘成一块被漏检 → 按网格补回来，
                # 否则大厅出现 N10 时会把 N9、N10 一起吞掉（2026-11-18 11:35 离线演练）
                rows = grid_complete_rows(img, rows, region, kw.get("bright", 26))
                n_now = len(rows)
                # 大厅的难度列表是一行一行画出来的，而且「新难度上线」时行数会变多
                # （v1.0.47 后大厅出现 N9）。只判「≥ min_rows」会在一行就点时把 N1 选走，
                # 所以要求连续两次抓到的行数一致，才算画完（用户 2026-11-18 10:33）。
                if n_now >= min_rows and (n_now == prev_len or time.time() >= deadline):
                    if attempt > 1:
                        log.info(f"难度列表画好了：{n_now} 行（等了 {attempt} 次，"
                                 f"连续两次一致）")
                    break
                if n_now > prev_len:
                    prev_len = n_now
                if time.time() >= deadline:
                    break
                # 这里不是「行数不够」，而是「还没连续两次抓到一样的行数」：
                # 大厅是一行一行画出来的，第一帧只画到 3 行就点会把 N1 选走。
                log.info(f"难度列表当前 {n_now} 行（要连续两次一致才算画完，"
                         f"下限 {min_rows} 行）→ 等 {int(retry_wait * 1000)}ms 再抓"
                         f"（第 {attempt} 次，最多等 {wait_rows:.0f}s）")
                self._sleep(retry_wait)

            if len(rows) < min_rows:
                log.warning(f"等了 {wait_rows:.0f}s 仍只识别到 {len(rows)} 行")
                self._dump_miss("diff_rows")
                # 2026-11-23 11:01 用户要求：英雄模式没难度（0 行）→ 点「普通模式」坐标 → 重新选一次
                fb = spec.get("fallback_click")
                if fb and not spec.get("_fb_used"):
                    log.info(f"英雄模式没难度 → 点普通模式 {fb} 后重选（2 次双击，间隔 0.5s）")
                    # 2026-11-23 11:11 用户要求：双击重复 2 次，间隔 0.5 秒
                    for _round in range(2):
                        self.input.click(int(fb[0]), int(fb[1]), "left", 0.08, 2, gap=0.12)
                        self._sleep(0.5)
                    self._sleep(1.5)
                    spec2 = dict(spec)
                    spec2["_fb_used"] = True
                    spec2["mode_base"] = 0      # 切到普通模式 → 难度名从 N1 起算
                    spec2.pop("fallback_click", None)
                    return self.exec_step({"click_row": spec2})
                # 兜底：按历史几何位置点（窗口固定 1690x921 时实测 第1行 y=127、行距 55.7）
                fb_first = spec.get("fallback_first_y")
                fb_pitch = spec.get("fallback_pitch")
                if want and fb_first and fb_pitch:
                    _reg = reg_spec or [int(690 * sx), int(105 * sy),
                                        int(190 * sx), int(470 * sy)]
                    x0, y0, rw, rh = [int(v) for v in _reg]
                    cx = x0 + rw // 2
                    cy = int(round((float(fb_first) + (want - 1) * float(fb_pitch)) * sy))
                    log.warning(f"→ 兜底按几何位置点第 {want} 行 ({cx},{cy})，"
                                f"点完靠自校验确认")
                    self.input.click(cx, cy, spec.get("button", "left"),
                                     float(spec.get("hold", 50)) / 1000.0, clicks,
                                     gap=float(spec.get("click_gap", 50)) / 1000.0)
                    self._sleep(float(spec.get("verify_wait", 700)) / 1000.0)
                    img2 = self.cap.grab(self.win)
                    rows2 = detect_rows(img2, region, **kw)
                    hl = highlight_index(rows2) if len(rows2) >= min_rows else None
                    digit, dscore = (read_digit(img2, rows2[hl if hl is not None else want - 1])
                                     if len(rows2) >= min_rows else (None, 0.0))
                    if hl is not None:
                        log.info(f"   兜底校验：高亮第 {hl + 1} 行"
                                 + ("  ✅ 与目标一致" if hl == want - 1 else f"  ⚠ 目标是第 {want} 行"))
                    if digit is not None:
                        log.info(f"   兜底校验：识别数字 {digit}（{dscore:.3f}）")
                    rec = {"rows": len(rows2), "picked_index": want, "target_row": want,
                           "text_guess": want_name or f"N{want}", "y": int(cy),
                           "hl_index": (hl + 1) if hl is not None else None,
                           "read_digit": digit, "digit_score": dscore,
                           "verified": (bool(digit == want) if digit is not None else None),
                           "fallback": True, "tag": tag,
                           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                    _p = dump_json_any(os.path.join("logs", "lobby_difficulty.json"), rec)
                    if _p is None:
                        log.warning("写难度记录失败（项目目录与临时目录都写不进）")
                    return (cx, cy)
                log.warning("→ 没有几何兜底数据，跳过不点（本轮难度未知）")
                return None

            # 可选：先滚到底。滚动后行签名不变 = 已经到底（或该列表根本不吃滚轮）
            if times > 0:
                x0, y0, rw, rh = [int(v) for v in region]
                sx, sy = x0 + rw // 2, y0 + rh // 2
                prev = None
                for i in range(times):
                    self.input.wheel(sx, sy, 1, -120)
                    self._sleep(float(spec.get("scroll_wait", 350)))
                    img2 = self.cap.grab(self.win)
                    rows2 = detect_rows(img2, region, **kw)
                    sig = [(r[0], r[1]) for r in rows2]
                    if prev is not None and sig == prev:
                        log.info(f"滚了 {i} 次后列表不再变化（到底或不可滚动），"
                                 f"当前可见 {len(rows2)} 行")
                        break
                    if len(rows2) < min_rows:
                        log.info(f"滚动后只识别到 {len(rows2)} 行，回退到滚动前的 {len(rows)} 行")
                        break
                    prev = sig
                    img, rows = img2, rows2

            # 落盘整幅 + 每行裁图：出问题能回看，也是「学数字模板」的素材来源
            try:
                dump_rows(img, rows, tag=tag, region=region)
            except Exception as e:
                log.warning(f"落盘难度列表失败（不影响开局）：{e}")

            # 大厅出现更高难度（例：v1.0.47 后多出 N9、以后还会有 N10）时：
            #   · 自动模式（want=None，没指定难度）→ 本来点「最下面一行」= 最高档，
            #     所以 N10 一出现就会选 N10，不需要额外处理；
            #   · 显式指定了难度（--difficulty N3 / --difficulty-seq）→ 听指令，不覆盖。
            #     以前这里会把 want 改成最高档，导致「刷低难度」被悄悄打成最高难度
            #     （2026-11-18 11:35 用户问 N10 时发现的连带问题）。
            new_top = None
            if want and len(rows) > want:
                new_top = len(rows)
                log.info(f"ℹ 大厅可见 {len(rows)} 行 > 本轮目标第 {want} 行"
                         f"（{want_name or 'N' + str(want)}）：本轮仍按指定点第 {want} 行；"
                         f"要「永远最高档」请不指定难度（自动模式会点第 {len(rows)} 行）")

            if want and want > len(rows):
                log.info(f"大厅只显示 {len(rows)} 个难度按钮（换账号从 N1 起、按进度解锁）"
                         f"→ 点第 {len(rows)} 个，按目标 {want_name or want} 记录")
                idx = len(rows) - 1
            elif want:
                idx = want - 1
            else:
                idx = len(rows) - 1
            if want:
                cx, cy = row_center(rows, idx)
            else:
                cx, cy = last_row_center(rows)
            hs = [b - t for (t, b, l, r) in rows]
            log.info(f"识别到 {len(rows)} 行，点第 {idx + 1} 行"
                     f"{'' if want else '（最下面一行=最高可见）'}"
                     f"→ {'双击' if clicks == 2 else '单击'} ({cx},{cy})  行高={hs}")
            # 难度只增不减（N1→…→N9→以后 N10/N11）：列表贴到识别区域底部时要提前提醒，
            # 否则下一档加进来会落在 region 外、静默识别不到。
            try:
                _gap = int(region[1]) + int(region[3]) - int(rows[-1][1])
                if _gap < 30:
                    log.warning(f"⚠ 难度列表快占满识别区域（最后一行底边距区域底只剩 {_gap}px）"
                                f"→ 再出新难度就要把 configs/wuxia_lobby.json 里 "
                                f"click_last_row 的 region 高度调大（当前 "
                                f"{int(region[3])}，每行约 53px）")
            except Exception:
                pass

            # 方案 3：把点到的难度按钮裁图存盘 + 和上一轮比字形（判断难度有没有推进）
            sim, p_tag = save_btn_crop(img, rows[idx], tag,
                                       learn_as=(want if want else None))
            if sim is not None:
                if sim > 0.93:
                    log.info(f"   🔍 难度按钮与上一轮（{p_tag}）相似度 {sim:.3f}"
                             f"  ⚠ 看起来还是同一个难度（本轮可能没推进）")
                else:
                    log.info(f"   🔍 难度按钮与上一轮（{p_tag}）相似度 {sim:.3f}"
                             f"  ✅ 与上一轮不同，难度已推进")

            # ---------------- 点一下 → 自校验（方案 3：截图识别文字 + 高亮行）----------------
            rec = {"rows": len(rows), "picked_index": idx + 1, "picked_row": idx + 1,
                   "target_row": want,
                   "mode_base": int(spec.get("mode_base", 0)),
                   "text_guess": (f"N{new_top + int(spec.get('mode_base', 0))}" if new_top
                                  else (want_name or f"N{idx + 1 + int(spec.get('mode_base', 0))}")),
                   "new_top_difficulty": new_top,
                   "y": int(cy), "row_heights": [int(x) for x in hs], "tag": tag,
                   "btn_sim_prev": sim, "btn_prev_tag": p_tag,
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            attempts = 1 + max(0, int(spec.get("verify_retry", 2)))
            hl = gl = digit = None
            hov = float(spec.get("hover", 700)) / 1000.0
            park = spec.get("park_xy") or [180, 800]
            for a in range(attempts):
                # War3 的难度列表行「直接 move+click 会被吞」：2026-11-18 10:40 实测，
                # 点第 9 行 N9 之后高亮还稳稳停在 N8，右侧「难度说明」也没变。
                # 必须先悬停 0.6~0.8s 让 War3 收到「鼠标进入按钮」，再按下才生效。
                self.input.click_hover(cx, cy, hover=hov,
                                       hold=float(spec.get("hold", 50)) / 1000.0,
                                       button=spec.get("button", "left"), clicks=clicks,
                                       gap=float(spec.get("click_gap", 60)) / 1000.0)
                try:
                    self._sleep(float(spec.get("verify_wait", 700)) / 1000.0)
                    # 校验截图前先把鼠标挪开：光标正压在刚点的那一行上，会把该行的
                    # 连通块撑大 / 和背景连成一片，detect_rows 直接少一行（实测 9→8），
                    # 于是校验被跳过、假报通过。
                    try:
                        self.input.move(int(park[0]), int(park[1]))
                        self._sleep(0.25)
                    except Exception:
                        pass
                    img2 = self.cap.grab(self.win)
                    rows2 = detect_rows(img2, region, **kw)
                    rows2 = grid_complete_rows(img2, rows2, region, kw.get("bright", 26))
                    dump_rows(img2, rows2, tag=f"{tag}_after", region=region)
                    hl = highlight_index(rows2)
                    gl = gold_highlight_index(img2, rows2)
                    base = gl if gl is not None else hl
                    digit, dscore = (read_digit(img2, rows2[base])
                                     if rows2 and base is not None else (None, 0.0))
                    rec["after_rows"] = len(rows2)
                    rec["after_heights"] = [int(b - t) for (t, b, l, r) in rows2]
                    rec["hl_index"] = (hl + 1) if hl is not None else None
                    rec["gold_index"] = (gl + 1) if gl is not None else None
                    rec["read_digit"] = digit
                    rec["digit_score"] = dscore
                    if len(rows2) != len(rows):
                        log.warning(f"   校验：点完识别到 {len(rows2)} 行（点前 {len(rows)} 行）"
                                    f"—— 高亮行会被画得更高更亮，行数变化属正常，按金色判据继续校验")
                    if base is not None:
                        log.info(f"   校验① 高亮行：第 {base + 1}/{len(rows2)} 行"
                                 f"（金色 {rec['gold_index']} / 行高 {rec['hl_index']}）"
                                 + ("  ✅ 与目标一致" if base == idx
                                    else f"  ⚠ 与目标第 {idx + 1} 行不一致"))
                    else:
                        log.info("   校验① 高亮行：判不出（既没有金色边框，行高差也太小）")
                    if digit is not None:
                        ok_d = (digit == idx + 1)
                        log.info(f"   校验② 识别数字：{digit}（{dscore:.3f}）"
                                 + ("  ✅ 与目标一致" if ok_d
                                    else f"  ⚠ 目标应为 {idx + 1}，数字对不上"))
                        rec["verified"] = bool(ok_d)
                    else:
                        log.info("   校验② 识别数字：还没有数字模板（templates/diff_digits）"
                                 "→ 裁图已落盘 logs/diff_rows，可据此补模板")
                        rec["verified"] = None
                except Exception as e:
                    log.warning(f"   校验失败（不影响开局）：{e!r}")
                    hl = gl = digit = None
                    base = None
                # 校验没过 → 再点一次（War3 第一次点击被吞是老问题）
                if base == idx or (digit is not None and digit == idx + 1):
                    break
                if a < attempts - 1:
                    log.info("   校验没过 → 再点一次（悬停后再按）")
            rec["click_count"] = a + 1

            # 把「这局选了什么难度」落盘，供 full_loop 判断低难度 N1~N6
            _p = dump_json_any(os.path.join("logs", "lobby_difficulty.json"), rec)
            if _p:
                log.info(f"难度记录：第 {rec['picked_index']}/{len(rows)} 行"
                         f" → 记为 {rec['text_guess']}（{_p}）")
            else:
                log.warning("写难度记录失败（不影响开局：两个目录都写不进）")
            return (cx, cy)

        # -- 文本输入（中文用 unicode，个别输入框不吃就用 paste） --
        if kind in ("type", "write"):
            spec = arg if isinstance(arg, dict) else {"text": arg}
            self.input.type_text(str(spec["text"]),
                                 float(spec.get("interval", 50)) / 1000.0,
                                 spec.get("method", "unicode"))
            return

        # -- 点进输入框再打字，并自校验是否真的填进去了（失败自动换 paste 重试） --
        if kind == "type_into":
            spec = arg
            text = str(spec["text"])
            region = spec.get("region")
            retries = int(spec.get("retries", 3))
            min_delta = float(spec.get("min_delta", 0.35))
            min_ratio = float(spec.get("min_ratio", 0.006))
            min_ink = int(spec.get("min_ink", 0))
            ok = False
            for attempt in range(retries):
                self._checkpoint()
                px = py = None
                if spec.get("template"):
                    t0 = time.time()
                    hit = None
                    while (time.time() - t0) * 1000 < float(spec.get("timeout", 5000)):
                        _, hit = self.vis.find_best(spec["template"], self.cap.grab(self.win),
                                                    float(spec.get("threshold", 0.85)),
                                                    spec.get("region_locate"),
                                                    spec.get("scales"))
                        if hit:
                            break
                        self._sleep(150)
                    if hit:
                        dx, dy = spec.get("offset", [0, 0])
                        px = hit[0] + hit[2] // 2 + int(dx)
                        py = hit[1] + hit[3] // 2 + int(dy)
                if px is None:
                    px, py = spec.get("point", [0, 0])
                    log.warning(f"用固定坐标点击输入框 ({px},{py})")
                self.input.click(int(px), int(py))
                self._sleep(int(spec.get("click_wait", 250)))
                before_patch = self._region_patch(region) if region else None
                before = float(before_patch.std()) if before_patch is not None else None
                method = spec.get("method", "unicode") if attempt == 0 else "paste"
                self.input.type_text(text, float(spec.get("interval", 80)) / 1000.0, method)
                self._sleep(int(spec.get("after_wait", 350)))
                after_patch = self._region_patch(region) if region else None
                after = float(after_patch.std()) if after_patch is not None else None
                if before is None or after is None:
                    log.warning(f"第 {attempt + 1} 次输入无法截图校验，重试")
                    continue
                _ratio = self._patch_diff_ratio(before_patch, after_patch)
                _ink = self._patch_ink(after_patch)
                # 双判据（2026-11-27 17:35 用户现场）：方差变化 + 像素变化占比都要达标。
                # 光标闪一下通常只有 0.1~0.4% 的像素变化，会被这里判成「没写进去」。
                if (abs(after - before) > min_delta and _ratio >= min_ratio
                        and _ink >= min_ink):
                    log.info(f"输入生效：{text!r}（第 {attempt + 1} 次，区域方差 "
                             f"{before:.2f}→{after:.2f}，变化像素 {_ratio * 100:.1f}%，"
                             f"内容像素 {_ink}px）")
                    ok = True
                    break
                log.warning(f"第 {attempt + 1} 次输入未生效（方差 {before:.2f}→{after:.2f}，"
                            f"变化像素 {_ratio * 100:.1f}% < {min_ratio * 100:.1f}%），"
                            f"换 paste 方式重试")
                # 重试前先清空：避免上一次「其实写进去了但被判失败」叠成 AaAa
                try:
                    for _k in range(6):
                        self.input.key("backspace", hold=0.03)
                        self._sleep(40)
                except Exception:
                    pass
            if not ok and spec.get("required", True):
                raise RuntimeError(f"输入失败，{retries} 次都没写进去: {text!r}")
            return

        # -- 点「创建」前的最后确认：密码框里必须真有内容（圆点/字符）--
        # 2026-11-27 17:35 用户现场：W10 密码框空着也点「创建」，开成无密码房。
        # 这道闸独立于 type_into —— 就算上面误判成功，这里也会把它拦下来。
        # 2026-11-27 17:44 用户要求：**发现是空的就继续输密码**（不是直接报错），
        #   补输最多 tries 次仍空着才抛错、不点「创建」。
        if kind in ("assert_ink", "check_ink", "ensure_pwd"):
            spec = arg if isinstance(arg, dict) else {}
            region = spec.get("region")
            if region:
                need = int(spec.get("min_ink", 40))
                text = spec.get("text")
                tries = int(spec.get("tries", 3))
                # 2026-11-27 17:48 用户要求：补输 3 次仍空着 → 点「取消」关掉弹窗
                # → 回详情页点「创建房间」重开 → 下一轮再填（最多 rounds 轮）。
                rounds = max(1, int(spec.get("rounds", 2)))
                for _rnd in range(1, rounds + 1):
                    patch = self._region_patch(region)
                    ink = self._patch_ink(patch)
                    attempt = 0
                    while ink < need and text and attempt < tries:
                        attempt += 1
                        log.warning(f"   ⚠️ 密码框还是空的（内容像素 {ink} < {need}）"
                                    f"→ 补输第 {attempt}/{tries} 次"
                                    + (f"（第 {_rnd}/{rounds} 轮）" if rounds > 1 else ""))
                        try:
                            for _k in range(8):          # 先清空，避免叠成 AaAa
                                self.input.key("backspace", hold=0.03)
                                self._sleep(35)
                        except Exception:
                            pass
                        px, py = spec.get("point", [0, 0])
                        try:
                            self.input.click(int(px), int(py))
                            self._sleep(int(spec.get("click_wait", 300)))
                            self.input.type_text(str(text),
                                                 float(spec.get("interval", 120)) / 1000.0,
                                                 "unicode")
                            self._sleep(int(spec.get("after_wait", 450)))
                        except Exception as e:
                            log.warning(f"   补输出错：{type(e).__name__}: {e}")
                        patch = self._region_patch(region)
                        ink = self._patch_ink(patch)
                    if patch is not None and ink >= need:
                        tail = f"，补输 {attempt} 次" if attempt else ""
                        log.info(f"   ✓ 密码框有内容（内容像素 {ink} ≥ {need}{tail}）")
                        return
                    if _rnd < rounds:
                        log.warning(f"   ⛔ 第 {_rnd} 轮补输 {tries} 次仍是空框"
                                    f" → 点「取消」重开建房弹窗再填")
                        if not self._cancel_and_reopen_dialog(spec):
                            raise RuntimeError("密码框校验失败，建房弹窗没重开 → 不点「创建」")
                    else:
                        raise RuntimeError(
                            f"密码框始终是空的（内容像素 {ink} < {need}，"
                            f"{rounds} 轮 × {tries} 次补输） → 不点「创建」")
            return

        # -- 启动外部程序（用户 2026-11-20 02:39：⑤ 点开始游戏时打开最小化工具）--
        if kind in ("start_app", "launch"):
            spec = arg if isinstance(arg, dict) else {"path": arg}
            start_app_external(spec)
            if spec.get("settle"):
                time.sleep(float(spec["settle"]))
            return

        # -- 循环 --
        if kind in ("loop", "repeat"):
            if isinstance(arg, list):
                body = arg
                count = int(step.get("count", 1))
                gap = float(step.get("gap", 0))
            else:
                body = arg.get("steps", [])
                count = int(arg.get("count", step.get("count", 1)))
                gap = float(arg.get("gap", step.get("gap", 0)))
            i = 0
            while count == 0 or i < count:      # count = 0 表示无限循环
                i += 1
                self.exec_steps(body)
                if gap:
                    self._sleep(gap)
            return

        raise ValueError(f"未知步骤类型: {kind}")


# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
def proc_running(exe_name: str) -> bool:
    """有没有同名进程在跑（start_app 防重复启动用；Toolhelp32 枚举，几毫秒）"""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x2

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                    ("th32ProcessID", ctypes.c_ulong),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                    ("th32ParentProcessID", ctypes.c_ulong),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
                    ("szExeFile", ctypes.c_char * 260)]

    target = exe_name.lower()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    try:
        e = PE32()
        e.dwSize = ctypes.sizeof(PE32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.decode("mbcs", "ignore").lower() == target:
                return True
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return False


def start_app_external(spec: dict) -> bool:
    """启动一个外部程序（start_app 步骤用，独立函数方便自检）。

    用户 2026-11-20 02:39：⑤ 大厅点「开始游戏」时同时打开
    「我在峡谷打僵尸最小化.exe」。DETACHED_PROCESS 启动后立刻返回，不挂住自动化链路。
    required=False（默认）时失败只告警，绝不打断任务；required=True 抛错。
    返回 True = 已启动。
    """
    path = os.path.normpath(os.path.expandvars(os.path.expanduser(str(spec["path"]))))
    # 2026-11-20 23:18 用户要求：工具可以放在**成品 exe 同目录**，成品搬到任意位置都能启动。
    # → 相对路径以「程序根目录」为基准解析（不是当前工作目录）：
    #    成品运行：exe 所在目录；源码运行：core 的上一级（项目根）
    if not os.path.isabs(path):
        if getattr(sys, "frozen", False):
            _root = os.path.dirname(sys.executable)          # 成品：exe 所在目录
        else:
            _root = os.path.dirname(os.path.abspath(__file__))  # 源码：脚本所在目录（项目根）
        path = os.path.join(_root, path)
    name = os.path.basename(path)
    # 已在运行就不重复启动（2026-11-20 02:41：⑤ 每局都跑，工具是常驻的话没必要每局开一个）
    if spec.get("skip_if_running", True):
        try:
            if proc_running(name):
                log.info(f"   🚀 {name} 已在运行 → 不重复启动")
                return True
        except Exception:
            pass
    try:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        subprocess.Popen([path], cwd=os.path.dirname(path) or None,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=0x00000008)   # DETACHED_PROCESS
        log.info(f"   🚀 已启动：{os.path.basename(path)}")
        return True
    except Exception as e:
        if spec.get("required", False):
            raise RuntimeError(f"启动失败：{path}（{type(e).__name__}: {e}）")
        log.warning(f"   ⚠️ 启动 {os.path.basename(path)} 失败"
                    f"（不影响继续）：{type(e).__name__}: {e}")
        return False


class _Log:
    def __init__(self):
        self.lines = []
        self.path = None

    def setup(self, log_dir: str):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(
            log_dir, datetime.now().strftime("bot_%Y%m%d_%H%M%S.log"))

    def _write(self, level: str, msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.lines.append(line)
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def info(self, msg):
        self._write("INFO", msg)

    def warning(self, msg):
        self._write("WARN", msg)

    def error(self, msg):
        self._write("ERR ", msg)


log = _Log()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def cmd_list_windows():
    rows = [w for w in enum_windows_full() if w["w"] > 120 and w["h"] > 120]
    rows.sort(key=lambda w: (w["exe"], -w["w"] * w["h"]))
    print(f"{'PID':>7}  {'EXE':<26} {'SIZE':>11}  TITLE")
    print("-" * 96)
    for w in rows:
        tag = "  [最小化]" if w["offscreen"] else ""
        print(f"{w['pid']:>7}  {w['exe']:<26} {w['w']:>5}x{w['h']:<5}  "
              f"{w['title'][:40]}{tag}")
    # 同时落盘：程序是窗口程序，很多电脑上控制台看不到，出问题直接把这个文件发出来
    try:
        os.makedirs("logs", exist_ok=True)
        with open(os.path.join("logs", "list_windows.txt"), "w", encoding="utf-8") as f:
            f.write(f"{'PID':>7}  {'EXE':<26} {'SIZE':>11}  TITLE\n")
            f.write("-" * 96 + "\n")
            for w in rows:
                tag = "  [最小化]" if w["offscreen"] else ""
                f.write(f"{w['pid']:>7}  {w['exe']:<26} {w['w']:>5}x{w['h']:<5}  "
                        f"{w['title'][:60]}{tag}\n")
        print("已写入 logs/list_windows.txt")
    except Exception as e:
        print(f"写 logs/list_windows.txt 失败：{e}")


def cmd_shot(args):
    enable_dpi_awareness()
    win = GameWindow(title_re=args.window_title, exe=args.window_exe)
    cap = Capture(args.capture)
    img = cap.grab(win)
    out = args.shot
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    ext = os.path.splitext(out)[1].lower() or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise RuntimeError("截图编码失败")
    buf.tofile(out)
    x, y, w, h = win.client_rect()
    print(f"已保存 {out}  尺寸={w}x{h}  客户区原点=({x},{y})")


def cmd_crop(args):
    """从整窗截图里裁一个矩形做模板：--crop x,y,w,h"""
    enable_dpi_awareness()
    win = GameWindow(title_re=args.window_title, exe=args.window_exe)
    img = Capture(args.capture).grab(win)
    x, y, w, h = [int(v) for v in args.crop.split(",")]
    patch = img[y:y + h, x:x + w]
    if patch.size == 0:
        raise RuntimeError("裁剪区域超出画面")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    ext = os.path.splitext(args.out)[1].lower() or ".png"
    ok, buf = cv2.imencode(ext, patch)
    buf.tofile(args.out)
    print(f"模板已保存 {args.out}  ({w}x{h})")


def cmd_probe(args):
    """打印若干坐标的像素值，用于配 if_pixel"""
    enable_dpi_awareness()
    win = GameWindow(title_re=args.window_title, exe=args.window_exe)
    img = Capture(args.capture).grab(win)
    x, y, w, h = win.client_rect()
    print(f"窗口客户区 {w}x{h}  原点=({x},{y})")
    for pt in args.probe.split(";"):
        px, py = [int(v) for v in pt.split(",")]
        b, g, r = img[py, px][:3]
        print(f"  ({px},{py})  BGR=({b},{g},{r})  HEX=#{r:02X}{g:02X}{b:02X}")


def cmd_run(args):
    enable_dpi_awareness()
    cfg_path = os.path.abspath(args.config)
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    log.setup(os.path.join(os.path.dirname(cfg_path), "..", "logs"))

    bot = Bot(cfg, os.path.dirname(cfg_path), dry_run=args.dry_run)
    bot.bind_hotkeys()

    task = args.task or cfg.get("start_task")
    if not task:
        raise SystemExit("没有指定任务：用 --task 或在配置里写 start_task")

    if not args.autostart:
        hk = cfg.get("hotkeys", {}).get("start_pause", "f8").upper()
        log.info(f"待命中，按 {hk} 开始执行任务 [{task}]")
        while not bot._started.wait(0.2):
            if not bot.win.alive():
                log.error("游戏窗口消失，退出")
                return 1
        time.sleep(0.2)

    try:
        bot.run_task(task)
    except StopBot:
        log.info("已按指令停止")
    except WindowLost as e:
        # 「退出游戏」这类任务的最后一步就是把窗口点掉，收尾的 wait 一定会撞上 WindowLost。
        # 这时不是失败，是成功——配置里用 "expect_window_close": true 声明。
        if (cfg.get("tasks", {}).get(task, {}) or {}).get("expect_window_close"):
            log.info(f"{e}（该任务预期会关窗口，视为成功）")
            return 0
        log.error(str(e))
        return 1
    except KeyboardInterrupt:
        log.info("Ctrl+C 中断")
    except Exception as e:
        log.error(f"运行异常: {type(e).__name__}: {e}")
        return 1
    finally:
        bot.input.release_all()
    return 0


def _find_room_by_size(rf):
    """W7 兜底：模板认不出房间窗时，**先按 PID + 尺寸配对**，配不上才逐个 activate 试探。

    2026-11-27 用户要求（现场 14:44）：W7 上不要为了截一张图把两扇平台窗反复置顶、
    任务栏来回切。所以第一步零截屏、零置顶：
      · 一扇 1332x945 主窗 + 一扇 1224x904 房间窗、且同 PID → 直接认定房间窗；
      · 配对不成立（多窗/尺寸不符）才逐个 activate 试探（这是最后手段）。
    """
    cands = [w for w in rf.platform_windows(min_client=300)
             if 1100 <= w["client"][2] <= 1350 and 820 <= w["client"][3] <= 980]
    cands.sort(key=lambda w: w["client"][2] * w["client"][3])   # 房间窗比详情页小，先试

    pair = _win7_room_pair(cands)
    if pair:
        log.info(f"   ✓ Win7 按 PID+尺寸配对命中房间窗 hwnd={pair['hwnd']}"
                 f"（PID={pair['pid']}，{pair['client'][2]}x{pair['client'][3]}）"
                 f"→ 不截屏、不置顶")
        return pair

    for w in cands:
        try:
            rf.activate(w["hwnd"], settle=0.6)
        except Exception:
            continue
        try:
            import plat_home as _ph
            face, _info = _ph.face_of(_ph.pg.grab_hwnd(w["hwnd"]))
            if face == "detail":
                continue
        except Exception:
            pass
        r = rf.find_room(verbose=False, restore=False)
        if r:
            return r
    return None


def _win7_room_pair(windows):
    """Win7/Win10 共用：仅当主窗和房间窗尺寸吻合且同进程时确认房间。"""
    mains = [w for w in windows
             if abs(w["client"][2] - 1332) <= 16
             and abs(w["client"][3] - 945) <= 16]
    rooms = [w for w in windows
             if abs(w["client"][2] - 1224) <= 16
             and abs(w["client"][3] - 904) <= 16]
    if len(mains) == len(rooms) == 1 and mains[0]["pid"] == rooms[0]["pid"]:
        return rooms[0]
    return None


def verify_room_before_start(verbose=True):
    """开局前校验房间：房间名必须是「我在峡谷打」、密码必须已设置。

    不合格（名字不对 / 密码没设）就点「退出」离开房间 → 后续 ①②③ 会重新建一个。
    这一步放在点「开始游戏」之前，因为房间进去之后才发现名字/密码不对就已经晚了。

    返回 'ok' / 'no_room' / 'reset' / 'unknown' / 'uncertain' / 'room_busy'
    """
    import room_finder as rf

    def restart_stuck(reason):
        """房间状态卡住时重启整个 KK 平台进程树。"""
        log.warning(f"   ♻️ {reason} → 结束 KK 平台所有相关进程并重新打开")
        rec = _restart_platform(verbose=verbose, force=True)
        status = (rec or {}).get("status")
        if status == "ok":
            log.info("   ✓ KK 平台已重开，准备重新查找房间")
            return "platform_restarted"
        if status == "login":
            log.warning("   KK 重开后出现登录窗口，等待手动登录")
            return "login"
        log.error(f"   ⛔ KK 重启失败：{status}；停止重复点击")
        return "room_busy"

    try:
        import sysenv as _se
        _w7 = _se.is_win7()
    except Exception:
        _w7 = False
    room, via = None, "template"
    candidates = []
    # 两个系统都先按窗口身份配对。find_room() 会逐个 TOPMOST 截图，
    # 双窗口已明确时调用它会在主窗和房间窗之间切屏。
    candidates = [w for w in rf.platform_windows(min_client=300)
                  if 1100 <= w["client"][2] <= 1350
                  and 820 <= w["client"][3] <= 980]
    pair = _win7_room_pair(candidates)
    if pair:
        room, via = pair, "window_pair"
        log.info(f"   ✓ KK 双窗口尺寸及进程匹配（同 PID={pair['pid']}），"
                 f"确认房间窗 hwnd={pair['hwnd']} → 不截屏、不置顶")
    if room is None:
        # 无法按几何确认时才回退到截图识别；句柄失效要当作没有房间。
        try:
            room = rf.find_room(verbose=False, restore=True)
        except Exception as _e_find:
            log.info(f"   房间探测出错（{_e_find!r}）→ 当作没有房间")
            room = None
    if not room and _w7:
        room = _find_room_by_size(rf)
        if room:
            via = "win7_activated"
        elif len(candidates) >= 2:
            log.error("   ⛔ Win7 检测到多扇平台窗口，且无法按 PID+尺寸配对"
                      " → 重启 KK 平台，清理重复窗口")
            return restart_stuck("Win7 多窗口反复切换且房间身份不明确")
    if not room:
        if verbose:
            log.info("   现在没有房间窗口 → 交给 ①②③ 新建")
        return "no_room"

    if _w7 and via == "window_pair":
        # 2026-11-27 用户定（现场 14:44 误判修复）：
        # 已经按 PID+尺寸确认是房间窗时，截图 0 分**只代表画面暂时读不到**，
        # 不代表房间「游戏中」。先绑定这扇窗交给第 ④ 步开局，
        # 不重启 KK、不重搜地图、不重建房间 —— 重建会白白丢掉房间。
        # 只有明确「开始游戏不可用 + 有退出按钮」才重启（is_room_busy 需要真实分数）。
        if rf.is_room_busy(room):
            return restart_stuck(f"Win7 房间 hwnd={room['hwnd']} 明确显示「游戏中」")
        BOUND_ROOM.update({"hwnd": room["hwnd"], "via": via, "scores": None})
        PREFER_HWND["hwnd"] = room["hwnd"]
        if verbose:
            log.info(f"   ✓ 已确认房间窗 hwnd={room['hwnd']} → 截图读不到不作为判据，"
                     "直接交给第 ④ 步开局（不重启 KK、不重建房间）")
        return "ok"

    if rf.is_room_busy(room):
        return restart_stuck(f"房间 hwnd={room['hwnd']} 显示游戏中")

    _h, info = rf.check_room(room["hwnd"], verbose=False)
    if info is None:
        if not _w7:
            log.error("   ⛔ Win10 房间名/密码无法读取，停止开局")
            return "uncertain"
        BOUND_ROOM.update({"hwnd": room["hwnd"], "via": via, "scores": None})
        if verbose:
            log.info("   ⚠️ 房间画面读不出来（可能是加载中/被遮挡）→ 复用已确认的房间窗口")
        return "unknown"

    name_ok, pwd_ok = info["name_ok"], info["pwd_ok"]
    if verbose:
        log.info(f"   房间校验：房间名 {'✔ 我在峡谷打' if name_ok else '✘ 不是「我在峡谷打」'}"
                 f"（{info['name_score']:.3f}）"
                 f" | 密码 {'✔ 已设置' if pwd_ok else '✘ 未设置'}"
                 f"（白点 {info['pwd_white']}px/{info['pwd_dots']} 个）")
    if name_ok and pwd_ok:
        BOUND_ROOM.update({"hwnd": room["hwnd"], "via": via, "scores": None})
        return "ok"

    why = []
    if not name_ok:
        why.append("房间名不是「我在峡谷打」")
    if not pwd_ok:
        why.append("房间密码没设置")
    log.info(f"   ⛔ {'、'.join(why)} → 退出房间，重新创建一个")
    r = rf.leave_room(room["hwnd"], verbose=verbose)
    log.info(f"   退出房间结果：{r}")
    if r in ("clicked", "no_room"):
        time.sleep(1.5)
        return "reset"
    # 2026-11-20 01:39 用户定：**退房 3 次无效（no_quit_btn）→ 直接重启平台**
    # 依据（2026-11-19 23:xx 日志分析 + README）：房间锁在「游戏中」状态时平台拒绝退房
    # ——日志实测退出按钮/确认框都 1.000 命中、点完房间还在；重试再多也没用，
    # README 明确记载「只能重启平台」。重启后房间窗口消失、房间自动释放。
    log.info("   ⛔ 退房 3 次无效（房间锁定在「游戏中」）→ 重启平台")
    rec = _restart_platform(verbose)
    st = (rec or {}).get("status")
    if st == "ok":
        log.info(f"   ✅ 平台已重启（{rec.get('seconds', 0)}s，大厅={rec.get('lobby')}）"
                 f" → 后续重新建房")
        return "reset"
    if st == "login":
        log.info("   ⛔ 重启后要登录/被顶号 → 停手（不自动登录）")
        return "login"
    log.info(f"   ⚠️ 重启平台没成功（{st}）→ 按原房间继续（兜底，可能再次卡死）")
    return "unknown"


RC_BUSY = 9       # 「您正在游戏中，不能创建游戏房间」等完还没释放 → 只能重启平台
RC_LOGIN = 131    # 自动重启平台后又弹「登录 / 顶号」→ 立刻停手（绝不自动登录）
RC_RESTARTED = 10  # 平台刚被自动重启 → 之前的房间/详情页全没了，整条链路要从第 1 步重来
RC_NODLG = 11      # 「创建房间」连点 3 次建房子弹窗都没出来 → 整条链路从第 1 步重跑一次


def _ensure_kk_ready():
    """启动时确保 KK 已运行：未运行则自动打开；登录页则停手等用户登录。

    返回 None = 就绪可以继续；否则返回该停手的返回码（RC_LOGIN / 1）。
    2026-11-27 用户要求（第 4 条）。
    """
    try:
        import plat_restart as _pr
        _ready = _pr.ensure_running(verbose=True) or {}
        _st = _ready.get("status")
        if _st == "login":
            log.error("⛔ KK 停在登录页 → 停止，请手动登录后再运行")
            return RC_LOGIN
        if _st != "ok":
            log.error(f"⛔ KK 未就绪：{_st}")
            return 1
    except Exception as e:
        log.info(f"   （自动打开 KK 的检查跳过：{e!r}）")
    return None
RC_BUSY_RETRY = 13  # 「您正在游戏中」→ 点确定 → 等完 → 从第 1 步（搜图）整段重来（尝试次数由上层数）
# 「创建房间」点击位被别的平台窗口（房间窗）盖住时，记下那扇窗的 hwnd。
# 2026-11-26 实测：退出游戏后房间窗常压在主窗上，按主窗坐标点下去全被它吃掉 →
# 584x517 建房弹窗永远不出来 → 连点 3 次 RC_NODLG → 房间⇄详情页死循环。
# 记到这里让 _open_create_dialog 直接判「房间还在」，跳过建房去开局。
ROOM_BLOCKED = {"hwnd": 0}


def _restart_platform(verbose=True, force=False):
    """自动重启平台（关掉 Platform.exe → 重新打开 → 等大厅）"""
    try:
        import plat_restart as pr
        return pr.restart(verbose=verbose, force=force)
    except Exception as e:
        log.info(f"   (重启模块出错：{type(e).__name__}: {e})")
        return dict(status="fail", why=f"{type(e).__name__}: {e}")


def _busy_hit():
    """有没有「平台提示 / 您正在游戏中，不能创建游戏房间」弹窗（僵尸会话）"""
    try:
        import busy_guard
        return busy_guard.find(verbose=False)
    except Exception as e:
        log.info(f"   (僵尸会话检测跳过：{type(e).__name__}: {e})")
        return None


def _busy_wait(minutes):
    """点掉弹窗并等 N 分钟，返回 busy_guard.wait_clear 的结果字符串"""
    try:
        import busy_guard
        return busy_guard.wait_clear(minutes)
    except Exception as e:
        log.info(f"   (等待出错，按已释放继续：{type(e).__name__}: {e})")
        return "clear"


def _close_platform_popups(verbose=False):
    """清掉挡在平台界面上的所有广告：主窗浮层（首充礼包 / 红包）+ 独立活动弹窗

    2026-11-27 00:10 用户要求：恢复广告弹窗的「点击」关闭（之前 11-26 停用过）。

    返回已处理项的中文名列表（空列表 = 没发现）。

    为什么要「点之前」清：这类广告是**模态**的 —— 弹着的时候点「选难度」「开始游戏」
    全都没反应，但视觉上不一定遮住那些按钮（2026-11-17 20:58 用户遇到的「216U币 红包」
    就是这样：按钮看得见、点不动）。所以只能先点 X 关掉，再点按钮。
    """
    acted = []
    try:
        import ad_guard
        r = ad_guard.close(verbose=verbose)
        if r == "closed":
            acted.append("广告浮层")
        elif r == "failed":
            log.info("   ⚠ 广告浮层没点掉，稍后会再试")
    except Exception as e:
        if verbose:
            log.info(f"   (广告浮层检测跳过：{type(e).__name__}: {e})")
    try:
        import popup_guard
        n = popup_guard.close_popups(max_n=3, quiet=not verbose)
        if n:
            acted.append(f"活动弹窗 x{n}")
    except Exception as e:
        if verbose:
            log.info(f"   (活动弹窗检测跳过：{type(e).__name__}: {e})")
    return acted




def _ad_check():
    """（旧入口）清一遍平台广告，返回是否处理过

    用户 2026-11-17 13:58：退出游戏 / 重开平台时会随机弹，直接点 X 关掉就行。
    它是主窗里的浮层，会吃掉下面所有点击 —— 当天第 1 轮就是被它挡着，
    点「创建房间」没反应，建房子弹窗一直不出来，整轮报废。
    """
    acted = _close_platform_popups(verbose=False)
    if acted:
        log.info(f"   🧹 关掉了挡在平台界面上的{'、'.join(acted)}（它会把下面的点击吃掉）")
        return True
    return False


def create_dialog_hwnd():
    """「创建房间」弹窗（独立小窗 584x517）在不在；不在返回 None。

    尺寸只认 420~800 × 350~660：主窗 1332x945、房间窗 1224x904、
    War3 大厅 1690x921 都落在范围外，不会被误当成弹窗。
    """
    try:
        for hwnd, _title, exe, w, h in enum_windows():
            if exe.lower() != "platform.exe":
                continue
            if 420 <= w <= 800 and 350 <= h <= 660:
                return hwnd
    except Exception:
        return None
    return None


def _open_create_dialog(base, tries=2, wait=4.0):
    """点「创建房间」并确认弹窗真的弹出来了；没弹就再点，最多 tries 次。

    2026-11-17 13:42 实测：第 1 轮详情页那一下点到了（score 0.905），
    但建房子弹窗没出来，紧接着 ③ 就绑到主窗、往固定坐标盲打 'Aa'，
    3 次全失败、整条链路报废。所以这里必须「点 → 等弹窗 → 没出来再点」。
    """
    h = create_dialog_hwnd()
    if h:
        return h
    # ② 那一步点「创建房间」时已经发现「点击位被房间窗盖住」→ 直接给结论，不再重复点。
    # hwnd 保留在 ROOM_BLOCKED 里，调用方跳 ④ 时要拿它绑房间窗。
    if ROOM_BLOCKED["hwnd"]:
        h = ROOM_BLOCKED["hwnd"]
        log.info(f"   ℹ 「创建房间」点不下去：房间窗 hwnd={h} 就在前台"
                 f"（房间没退）→ 不新建，直接复用它开局")
        return "room_alive"
    cfg_path = os.path.join(base, "configs", "wuxia.json")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "configs", "wuxia.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in range(1, tries + 1):
        try:
            bot = Bot(cfg, os.path.dirname(cfg_path), dry_run=False)
            bot.bind_hotkeys()
            try:
                bot.run_task("create_room")
            finally:
                bot.input.release_all()
        except Exception as e:
            log.info(f"   第 {k} 次点「创建房间」出错：{type(e).__name__}: {e}")
        # 第 k 次点击被房间窗挡住（点击位最上层不是主窗）→ 房间其实还在，
        # 别再连点（点多少次都打不中），把「房间还在」这个结论交回 pipeline 去复用。
        if ROOM_BLOCKED["hwnd"]:
            h = ROOM_BLOCKED["hwnd"]
            log.info(f"   ℹ 「创建房间」点不下去：房间窗 hwnd={h} 就在前台"
                     f"（房间没退）→ 不新建，直接复用它开局")
            return "room_alive"
        t0 = time.time()
        while time.time() - t0 < wait:
            h = create_dialog_hwnd()
            if h:
                log.info(f"   ✓ 建房子弹窗已出现（hwnd={h}，第 {k} 次点击）")
                return h
            time.sleep(0.3)
        log.info(f"   ⚠️ 第 {k}/{tries} 次点「创建房间」后弹窗没出现 → 再点一次")
    # 2026-11-27 10:00：所有尝试都没弹出 584x517 → 如果平台上还有「房间尺寸」的窗口，
    # 就是房间没退（W7 上被详情页盖住时模板认不出）。先 activate 再判形态，
    # 不是详情页就交给上层跳 ④ 复用开局，别再连点/重启平台。
    try:
        import room_finder as _rf_g
        _cands = [w for w in _rf_g.platform_windows(min_client=300)
                  if 1100 <= w["client"][2] <= 1350 and 820 <= w["client"][3] <= 980]
        _cands.sort(key=lambda w: w["client"][2] * w["client"][3])
        for w in _cands:
            try:
                _rf_g.activate(w["hwnd"], settle=0.6)
            except Exception:
                continue
            try:
                import plat_home as _ph
                _f, _i = _ph.face_of(_ph.pg.grab_hwnd(w["hwnd"]))
            except Exception:
                _f = "unknown"
            if _f == "detail":
                continue
            ROOM_BLOCKED["hwnd"] = w["hwnd"]
            log.info(f"   ℹ 建房弹窗没出来，但平台还有 {w['client'][2]}x{w['client'][3]} 的窗口"
                     f"（不是详情页）→ 判定房间还在，复用它开局")
            return "room_alive"
    except Exception:
        pass
    return None


def _reopen_target(steps, idx):
    """「重新开局」该回退到哪一步：当前步之前最后一个 activate_room（房间点开始游戏）。

    用户 2026-11-18 23:05：手动结束 War3 后平台弹断线重连，弹窗关掉这一局就彻底结束了，
    大厅永远等不回来 → 回退到「房间开局」重开，而不是干等 15 分钟。
    找不到（本次只跑了 ⑤ 这种且前面没有开局步）返回 None，交给上层判失败。
    当前步自己就是开局步（④ 点完开始游戏后 War3 没起来）时返回自己 —— 允许原地重试，
    次数由 reopen_on_game_gone 兜住，试完就判失败，不会死循环。
    """
    for j in range(int(idx), -1, -1):
        if steps[j].get("activate_room"):
            return j
    return None


# 点完「创建」后固定先等这么久再开始探房间窗（用户 2026-11-26 要求 5s）。
# 平台要销毁弹窗、把前台交还主窗、再新建 1224x904 房间窗，这段内容渲染约 9~12s，
# 先静默 5s 再探测，能少打几轮空转。
ROOM_FRONT_DELAY = 6.0

# ---- 「按新窗口绑房间」（2026-11-26 18:57 用户定的双保险方案）----
# SNAP：点「创建」之前所有平台窗口的 hwnd 集合 —— 建房后**新出现**的那扇就是房间窗。
#       只枚举窗口，不截屏，所以记录本身零开销、零闪烁。
# BOUND_ROOM：已绑定的房间 hwnd 与来源。④ 直接用它，不再按尺寸/模板猜
#       （详情页和房间窗都是 1224x904，按尺寸猜在 W7 上绑错过一次）。
# PREFER_HWND：GameWindow 挑窗时的首选句柄（只在候选里真有这扇窗时才生效）。
_SNAP = {"hwnds": None}
BOUND_ROOM = {"hwnd": None, "via": None, "scores": None}
PREFER_HWND = {"hwnd": None}


def snapshot_room_windows():
    """点「创建」之前记下当前平台窗口 hwnd 集合（毫秒级，不截屏、不置顶）"""
    try:
        import room_finder as _rf
        _SNAP["hwnds"] = {w["hwnd"] for w in _rf.platform_windows(min_client=300)}
    except Exception:
        _SNAP["hwnds"] = None
    # 新一轮建房开始，上一局按新窗口绑的 hwnd 作废
    BOUND_ROOM["hwnd"] = None
    BOUND_ROOM["via"] = None
    BOUND_ROOM["scores"] = None


def await_room_front():
    """③ 点完「创建」静默 6s 后绑房间窗 —— 双保险（2026-11-26 18:57 用户定）：

    路径 ①（新窗口法，首选）：点「创建」前记过 hwnd 集合，之后**新出现**的那扇
      平台窗口就是房间窗 —— 纯窗口枚举判定，不截屏、不置顶，**零闪烁**，
      也不受 W7 渲染慢影响（窗口一出现就能绑，不用等页面画完）。
      绑之前用 peek_window 静默看一眼（PrintWindow，不改前后顺序）做保险：
      新窗里还长着「创建房间」按钮 = 详情页（建房没成）→ 不绑它，退回模板法。
      画面完全拿不到（W7 黑图）就信新窗口直接绑。
    路径 ②（模板法，兜底）：老 find_room —— 房间号 ≥0.90 且 开始游戏 ≥0.72。
      W7 字体渲染分数偏低时这条可能认不出 → 返回 None，④ 门禁判整段重来。

    返回 (房间信息 dict 或 None, 耗时秒)。
    """
    t0 = time.time()
    try:
        import room_finder as _rf
    except Exception as e:
        log.info(f"   (房间探测不可用：{e!r})")
        return None, 0.0

    def _bind(r, via, scores=None):
        try:
            _rf.activate(r["hwnd"])
        except Exception:
            pass
        BOUND_ROOM["hwnd"] = r["hwnd"]
        BOUND_ROOM["via"] = via
        BOUND_ROOM["scores"] = scores
        return r, time.time() - t0

    snap = _SNAP.get("hwnds")
    _SNAP["hwnds"] = None                     # 用完即弃，防止跨局误判
    if snap:
        try:
            news = [w for w in _rf.platform_windows(min_client=400)
                    if w["hwnd"] not in snap]
        except Exception:
            news = []
        news.sort(key=lambda w: (w["client"][2] or 0) * (w["client"][3] or 0))
        for w in news:
            try:
                info = _rf.peek_window(w["hwnd"])
            except Exception:
                info = None
            if info is None:
                log.info(f"   ✓ 新窗口 hwnd={w['hwnd']} "
                         f"{w['client'][2]}x{w['client'][3]}"
                         f"（画面拿不到 → 按新窗口直接绑）")
                return _bind(w, "new_window")
            s_no, s_start, s_create = info
            if s_create >= 0.80:
                log.info(f"   ⚠ 新窗口 hwnd={w['hwnd']} 里还是「创建房间」按钮"
                         f"（score={s_create:.3f}）→ 建房没成，这不是房间窗")
                continue
            log.info(f"   ✓ 新窗口 hwnd={w['hwnd']} {w['client'][2]}x{w['client'][3]}"
                     f" 房间号={s_no:.3f} 开始游戏={s_start:.3f}"
                     f"（按新窗口绑定，未置顶截屏）")
            return _bind(w, "new_window", (s_no, s_start))
        if news:
            log.info("   ⚠ 新出现的窗口都是详情页 → 没有房间窗，退回模板识别")

    # 路径 ②：模板识别（老路径兜底）
    try:
        r = _rf.find_room(verbose=False)
    except Exception:
        r = None
    if r:
        return _bind(r, "template")
    return None, time.time() - t0


def lower_priority() -> bool:
    """把本进程降到「低于正常」优先级（2026-11-19 降 CPU / 让 War3 更顺）

    只影响调度顺序：本进程主要花在截图 + 模板匹配的短突发上，降到 BELOW_NORMAL
    后 War3 抢 CPU 更顺，而本进程的操作延迟变化在毫秒级（点按钮/找图实测无感）。
    不碰任何进程的启停，也不发任何关闭指令。
    """
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        # ⚠ 64 位必须显式声明：ctypes 默认把返回值当 c_int，
        # GetCurrentProcess() 的伪句柄会被截成 32 位 → SetPriorityClass 拿到无效句柄
        # 直接失败返回 0（2026-11-19 自检抓到：返回 False 且优先级仍是 0x0000）。
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass.restype = ctypes.c_int
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        return bool(k32.SetPriorityClass(k32.GetCurrentProcess(),
                                         BELOW_NORMAL_PRIORITY_CLASS))
    except Exception:
        return False


def cmd_pipeline(args):
    """一条命令跑完整链路：按 pipeline 配置依次执行多个 (config, task)。

    每一步都会重新绑定自己的窗口（平台主窗 / 弹窗 / 房间 / War3 大厅），
    所以比 bat 里串 4 条命令更稳，也省掉每次进程启动的 1~2 秒。"""
    import sysenv as _se
    _w7 = _se.is_win7()
    enable_dpi_awareness()
    # 2026-11-27 用户要求（第 4 条）：KK 未运行 → 自动打开；登录页 → 停下等用户登录。
    # ensure_running 幂等，full_loop 已调过也不会重复拉起。
    _rc0 = _ensure_kk_ready()
    if _rc0 is not None:
        return _rc0
    if lower_priority():                 # 降 CPU：不跟 War3 抢 CPU
        log.info("⚙ 本进程已设为「低于正常」优先级（识别不受影响，War3 更顺）")
    p_path = os.path.abspath(args.pipeline)
    with open(p_path, "r", encoding="utf-8") as f:
        pcfg = json.load(f)
    base = os.path.dirname(p_path)
    log.setup(os.path.join(base, "..", "logs"))

    steps = pcfg.get("steps", [])
    if not steps:
        log.error("pipeline 里没有 steps")
        return 1

    only = [s.strip() for s in (args.only or "").replace("，", ",").split(",") if s.strip()]

    # 开局前先校验房间（房间名「我在峡谷打」+ 密码已设置），不合格就退房重建。
    # 只在本次要跑「房间相关步骤」时做（--only ⑤ 这种就没必要）。
    def _selected(st):
        name = st.get("name") or ""
        return (not only or any(k in name for k in only))

    need_room = any(_selected(st) and (st.get("skip_if_room") or st.get("activate_room"))
                    for st in steps)
    if need_room and not getattr(args, "no_room_check", False):
        log.info("━━ 开局前房间校验（房间名 / 密码）━━")
        try:
            _vr = verify_room_before_start()
            if _vr == "login":
                log.info("   ⛔ 房间校验阶段重启平台后出现登录/顶号 → 本次链路停手")
                return RC_LOGIN
            if _vr == "game_running":
                log.info("   ✓ 已恢复/复用当前游戏，跳过创建房间和开始游戏")
                return 0
            if _vr == "platform_restarted":
                return RC_RESTARTED
            if _vr in ("uncertain", "room_busy") or (_vr == "unknown" and not _w7):
                return 1
        except Exception as e:
            log.error(f"   ⛔ 房间校验出错，停止本次开局：{type(e).__name__}: {e}")
            return 1

    # 地图更新守卫（用户 2026-11-18 10:24：「更新问题弹窗的处理办法，以后遇见直接处理」）。
    # 「平台提示 / 地图已更新，请重新下载地图」只在退出游戏 / 开局时弹，而且点「确定」
    # 只是关提示 —— 地图并不会下载，接着点「开始游戏」War3 会秒退。
    # 真正下载发生在「创建房间 → 点创建」那一步，所以这里命中就：关提示 → 退房 → 重建房间。
    if not getattr(args, "no_map_update", False):
        try:
            import map_update as _mu
            _mu_hit = _mu.find(verbose=False)
            if _mu_hit:
                log.info("━━ 检测到「地图已更新，请重新下载地图」"
                         f"（{_mu_hit['w']}x{_mu_hit['h']} score={_mu_hit['score']:.3f}）━━")
                log.info("   处理：点「确定」关提示 → 重建房间（这一步才真正下载新版地图）")
                _mu_fix = _mu.fix(verbose=True)
                log.info(f"   地图更新处理结果：{_mu_fix.get('status')}（{_mu_fix}）")
                if _mu_fix.get("status") not in ("fixed", "no_popup_no_action"):
                    log.error("   ⛔ 地图更新没处理好 → 停止本次链路"
                              "（带着旧地图开局必然秒退，不浪费一轮）")
                    return 1
                log.info("━━ 地图更新处理完毕，继续开局 ━━")
        except Exception as e:
            log.info(f"   (地图更新守卫出错，按原样继续：{type(e).__name__}: {e})")

    t_all = time.time()
    done = 0

    # 僵尸会话（强杀 War3 后平台仍认为「还在游戏中」）要等多久。
    # 优先级：命令行 > pipeline.json > 默认 6 分钟。
    # 2026-11-17 15:07 用户改口径：「从第一步搜索地图开始到创建房间」算一次尝试，共 4 次、每次间隔 6 分钟，
    # 4 次都不行才重启平台 —— 所以尝试计数交给上层（full_loop），这里只负责「点确定 + 等完 + 整段重来」。
    busy_wait = pcfg.get("busy_wait_minutes", 6)
    if getattr(args, "busy_wait", None) is not None:
        busy_wait = args.busy_wait
    # --no-busy-wait 才是「完全不看这个弹窗」；--busy-wait 0 = 照旧检测+点确定，只是不等
    busy_check = not getattr(args, "no_busy_wait", False)
    if busy_wait in (None, ""):
        busy_wait = 0
    busy_wait = max(0.0, float(busy_wait or 0))
    # 兼容旧参数（已不再使用）：--busy-retries / --busy-restart 仍可传，但不影响行为
    if busy_check:
        log.info(f"   僵尸会话策略：命中「您正在游戏中」→ 点「确定」→ 等 {busy_wait:g} 分钟"
                 f" → 从第 1 步（搜图）整段重来（整段尝试次数由上层控制）")

    idx = 0
    # 开局前已确认房间时直接进入房间开局；不再经过搜地图/创建房间，
    # 避免 Win7 截图偶发失败后又把同一扇房间窗当成「新建失败」。
    if BOUND_ROOM.get("hwnd"):
        room_step = next((k for k, s in enumerate(steps) if s.get("activate_room")), None)
        if room_step is not None:
            log.info(f"   ⏭ 已绑定房间 hwnd={BOUND_ROOM['hwnd']}"
                     f"（来源={BOUND_ROOM['via']}）→ 直接进入第 {room_step + 1} 步开局")
            idx = room_step
    # ⑤ 撞上「这一局已经结束」（断线重连关掉后发现 War3 进程没了）时，
    # 允许回退到「房间开局」那一步重新点开始游戏。次数由 pipeline.json 的
    # reopen_on_game_gone 控制（缺省 2 次），防止反复重开死循环。
    reopen_left = int(pcfg.get("reopen_on_game_gone", 2) or 0)
    # 2026-11-27 16:31 用户定：回不到 ④ / 房间窗确实没了 → 重新 ①②③ 建房。
    # 单独计数，避免和「重开一局」抢配额，也防无限重建。
    rebuild_left = int(pcfg.get("rebuild_on_room_gone", 2) or 0)
    # 2026-11-27 17:35：②「房间还在就复用」这条捷径必须先校验房间名/密码。
    # 现场（W10 17:25）连点 3 次没出建房弹窗 → 直接跳 ④ 开局，③ 填密码整步被跳过，
    # 等价于拿上一轮的旧房间（可能没密码）开局。校验不合格就退回 ① 重建，最多 2 次。
    _reuse_bad = 0

    def _reuse_room_check():
        log.info("   🔎 复用房间前重新校验（房间名 / 密码）")
        try:
            return verify_room_before_start(verbose=True)
        except Exception as e:
            log.error(f"   复用校验出错：{type(e).__name__}: {e}")
            return "unknown"

    def _back_to_fill_pwd():
        """校验不合格后回「③ 填密码并提交」重建；没有这一步就回第 1 步搜地图。"""
        for k, s2 in enumerate(steps):
            if "③" in (s2.get("name") or ""):
                return k
        return 0

    loops = 0
    while idx < len(steps):
        st = steps[idx]
        i = idx + 1
        skipped = False
        loops += 1
        # 每步最多允许「重开 + 跳过 + 正常」折腾 10 次；超了说明下标没推进（死循环），
        # 直接判失败，别让成品在那儿空转一晚上。
        if loops > max(20, len(steps) * 10):
            log.error(f"⛔ pipeline 循环 {loops} 次仍未跑完 {len(steps)} 步"
                      f"（当前第 {i} 步「{st.get('name')}」）→ 按下标未推进处理，停止")
            return 1
        name = st.get("name") or f"{os.path.basename(st['config'])}:{st['task']}"
        if only and not any(k in name for k in only):
            idx += 1
            continue

        # 已有房间就别再建房（重复建房会弹「您正在房间内…」，点错了就直接解散房间）
        if st.get("skip_if_room") or st.get("activate_room"):
            _room = None
            # ③ 之后按「新窗口法」绑过的 hwnd 直接用 —— 不再截屏认一遍。
            # W7 字体渲染分数偏低，模板法会把明明绑上的房间判成「非房间」。
            if st.get("activate_room") and BOUND_ROOM.get("hwnd"):
                try:
                    if ctypes.windll.user32.IsWindow(BOUND_ROOM["hwnd"]):
                        _room = {"hwnd": BOUND_ROOM["hwnd"]}
                except Exception:
                    _room = None
            if _room is not None:
                try:
                    import room_finder as _rf
                    _rf.activate(_room["hwnd"])
                except Exception:
                    pass
                log.info(f"   ⏭  {name}：直接用已绑定的房间窗 hwnd={_room['hwnd']}"
                         f"（来源={BOUND_ROOM.get('via')}，不重复识别）")
            else:
                try:
                    import room_finder as _rf
                    _room = _rf.find_room(verbose=False)
                    if _room:
                        if st.get("activate_room"):
                            _rf.activate(_room["hwnd"])
                        if st.get("skip_if_room"):
                            log.info(f"   ⏭  {name} 跳过：已存在房间窗口 hwnd={_room['hwnd']}"
                                     f"（{_room['client'][2]}x{_room['client'][3]}），直接复用")
                            skipped = True
                except Exception as e:
                    log.info(f"   (房间探测失败，按无房间处理：{e})")
            # 2026-11-26 18:37 用户要求：这一步必须在**房间窗**里做（双击/观战/开始游戏）。
            # 认不到房间窗就绝不能按 pick（smallest）硬绑 —— 详情页也是 1224x904，
            # 绑上去之后观战框、开始游戏全找不到，还白点一通（Win7 18:31 那轮就是这样）。
            if st.get("activate_room") and not _room:
                log.error(f"   ⛔ {name}：没认到房间窗（房间号/开始游戏都对不上）"
                          f" → 不拿详情页硬绑，整条链路从第 1 步（搜地图）重来")
                return RC_NODLG
            if skipped:
                idx += 1
                continue

        cfg_path = os.path.abspath(os.path.join(base, st["config"]))
        if not os.path.exists(cfg_path):          # 兼容 "configs/x.json" 这种相对项目根的写法
            cfg_path = os.path.abspath(st["config"])
        if not os.path.exists(cfg_path):
            log.error(f"找不到配置：{st['config']}（pipeline: {p_path}）")
            return 1
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        log.info(f"━━ 第 {i}/{len(steps)} 步：{name} ━━")
        # ④「点开始游戏」前最后一道闸：房间名必须是「我在峡谷打」、密码必须已设置。
        # 2026-11-27 17:35 用户现场（W10 17:25）：②连点 3 次没出建房弹窗 → 直接跳 ④，
        # ③ 填密码整步被跳过，等于没设密码就开局。这里点之前再验一次。
        if st.get("activate_room") and not args.dry_run:
            _v = _reuse_room_check()
            if _v in ("reset", "no_room"):
                _reuse_bad += 1
                if _reuse_bad > 2:
                    log.error(f"   ⛔ 开局前房间校验连续 {_reuse_bad} 次不合格（{_v}）→ 停止本轮")
                    return RC_NODLG
                log.warning(f"   ⚠️ 开局前房间校验不合格（{_v}）→ 已退房，回第 "
                            f"{_back_to_fill_pwd() + 1} 步重新填密码建房"
                            f"（第 {_reuse_bad}/2 次）")
                BOUND_ROOM.update({"hwnd": None, "via": None, "scores": None})
                PREFER_HWND["hwnd"] = None
                idx = _back_to_fill_pwd()
                continue
            if _v == "login":
                return RC_LOGIN
            if _v == "platform_restarted":
                return RC_RESTARTED
            if _v == "game_running":
                return 0
            if _v in ("unknown", "uncertain", "room_busy"):
                if not _w7:
                    log.error(f"   ⛔ Win10 开局前房间名/密码未能确认（{_v}），停止开局")
                    return 1
                log.info(f"   ℹ 房间状态判不了（{_v}）→ 按 W7 优先放行，继续 ④")
        # 每步开跑前先扫一眼广告浮层（首充礼包这类会盖住主窗、吃掉所有点击）
        _ad_check()
        # ③ 之前必须先确认建房子弹窗真的在：不在就补点「创建房间」，
        # 绝不允许再拿主窗当弹窗、往固定坐标盲打密码（2026-11-17 13:42 就是这么废掉一轮的）
        if "③" in name and not create_dialog_hwnd():
            log.info("   🔎 进 ③ 前没看到建房子弹窗 → 补点「创建房间」")
            _oc = _open_create_dialog(base, tries=2)
            if _oc == "room_alive":
                _rh = ROOM_BLOCKED["hwnd"] or 0
                ROOM_BLOCKED["hwnd"] = 0
                if _rh:
                    # 把这扇「盖在主窗上的房间窗」直接登记好，④ 绑窗时优先用它
                    # （W7 上房间画面可能还没刷新，模板认不出，不能靠 find_room 再找一遍）
                    BOUND_ROOM.update({"hwnd": _rh, "via": "room_alive", "scores": None})
                    PREFER_HWND["hwnd"] = _rh
                _back = next((k for k, s2 in enumerate(steps) if s2.get("activate_room")), None)
                if _back is not None:
                    # 复用旧房间前必须校验房间名/密码（否则 ③ 填密码会被整步跳过）
                    _v = _reuse_room_check()
                    if _v == "ok":
                        log.info(f"   ⏭ 房间窗其实还在（就压在主窗上）→ 不建房，直接回第 "
                                 f"{_back + 1} 步「{steps[_back].get('name')}」开局")
                        idx = _back
                        continue
                    if _v == "login":
                        return RC_LOGIN
                    if _v == "platform_restarted":
                        return RC_RESTARTED
                    if _v == "game_running":
                        return 0
                    _reuse_bad += 1
                    if _reuse_bad > 2:
                        log.error(f"   ⛔ 复用的房间连续 {_reuse_bad} 次不合格（{_v}）→ 停止本轮")
                        return RC_NODLG
                    log.warning(f"   ⚠️ 复用的房间不合格（{_v}）→ 已退房，回第 "
                                f"{_back_to_fill_pwd() + 1} 步重新填密码建房"
                                f"（第 {_reuse_bad}/2 次）")
                    BOUND_ROOM.update({"hwnd": None, "via": None, "scores": None})
                    PREFER_HWND["hwnd"] = None
                    idx = _back_to_fill_pwd()
                    continue
                log.error("   ⛔ 房间还在，但 pipeline 里没有「房间内开局」那一步 → 无法复用")
                return RC_NODLG
            if not _oc:
                log.error("   ⛔ 连点 3 次「创建房间」，建房子弹窗始终没出来")
                log.error("   ↻ 详情页很可能已经不是刚搜出来的那个（浏览器缓存/加载慢）→ 整条链路从第 1 步重跑")
                return RC_NODLG
        reopened = False
        # 「按新窗口绑房间」的钩子（2026-11-26 18:57 用户定）：
        #  · ③（填密码并提交）开跑前记下所有平台窗口 hwnd —— 点完「创建」后
        #    新出现的那扇就是房间窗，await_room_front 按差集绑，零截屏零闪烁；
        #  · ④ 直接用 ③ 绑好的 hwnd，让 GameWindow 优先挑它（不再按尺寸猜，
        #    详情页和房间窗都是 1224x904）。
        PREFER_HWND["hwnd"] = None
        if "③" in name and not args.dry_run:
            snapshot_room_windows()
        elif st.get("activate_room") and BOUND_ROOM.get("hwnd"):
            PREFER_HWND["hwnd"] = BOUND_ROOM["hwnd"]
        while True:
            t0 = time.time()
            bot = Bot(cfg, os.path.dirname(cfg_path), dry_run=args.dry_run)
            bot.bind_hotkeys()
            status, err = "ok", None
            try:
                bot.run_task(st["task"])
            except StopBot:
                log.info("已按指令停止整条链路")
                return 0
            except WindowLost as e:
                # 弹窗类步骤（比如填密码点「创建」）结束时窗口本来就会关闭，
                # 这不代表失败——下一步会重新绑定它该用的那个窗口。
                log.info(f"   {name} 结束时原窗口已关闭（{e}），视为正常切换，继续下一步")
            except GameGone as e:
                status, err = "gone", e
            except PlatformRestarted as e:
                log.info(f"   ♻️ {e}")
                return RC_RESTARTED
            except PlatformLoginRequired as e:
                log.info(f"   ⛔ {e}")
                return RC_LOGIN
            except KeyboardInterrupt:
                log.info("Ctrl+C 中断整条链路")
                return 1
            except Exception as e:
                status, err = "failed", e
            finally:
                bot.input.release_all()

            # ③「填密码」失败最常见的原因是：房间其实已经存在了，点「创建房间」被
            # 「您正在房间内…」拦下、根本没弹建房子窗口，于是绑到了主窗口空点。
            # 这种情况房间是好的，不该整条链路陪葬 → 直接进 ④。
            if status == "failed" and "③" in name:
                # 2026-11-27 17:42 用户要求：密码没写进去就**不能**当「房间已存在」跳过，
                # 否则又变成拿无密码房开局。直接判失败交上层重试。
                if "密码框" in str(err) or "输入失败" in str(err):
                    log.error(f"   ⛔ {name}：{err} → 密码没写进去，不拿这个房间开局")
                    return 1
                try:
                    import room_finder as _rf
                    _r = _rf.find_room(verbose=False)
                except Exception:
                    _r = None
                if _r:
                    log.info(f"   ⚠️ {name} 失败（{err}），但房间已在（hwnd={_r['hwnd']}）"
                             f" → 跳过这步，直接进下一步")
                    status = "ok"
            if status == "failed" and st.get("optional"):
                log.error(f"{name} 失败: {type(err).__name__}: {err}")
                log.info("   该步标了 optional，继续往下走")
                status = "ok"

            # 僵尸会话：不管这步算成功还是失败，只要弹了「您正在游戏中，不能创建游戏房间」，
            # 就点「确定」→ 等 N 分钟（平台要自己超时才放行）→ 退出本进程，由上层从第 1 步整段重来。
            # 用户 2026-11-17 15:07：一次尝试 = 「搜图 → 进详情页 → 建房」，共 4 次、间隔 6 分钟，
            # 4 次都不行才重启平台；所以这里不做原地重试、也不自己重启平台。
            if busy_check:
                hit = _busy_hit()
                if hit:
                    log.info(f"   ⚠️ 检测到「您正在游戏中，不能创建游戏房间」"
                             f"（score={hit['score']:.3f}，窗口 {hit['w']}x{hit['h']}）")
                    if busy_wait > 0:
                        log.info(f"   → 点「确定」→ 等 {busy_wait:g} 分钟让平台释放游戏会话，"
                                 f"然后从第 1 步（搜索地图）整段重来")
                        r = _busy_wait(busy_wait)
                    else:
                        # 最后一次尝试：不等了，点掉弹窗直接交回上层（上层会重启平台）
                        log.info("   → 点「确定」（最后一次尝试，不再等待）→ 交回上层重启平台")
                        try:
                            import busy_guard as _bg
                            _bg.dismiss(verbose=True)
                        except Exception:
                            pass
                        r = "clear"
                    if r in ("stopped", "login"):
                        log.error(f"   等待被打断（{r}）→ 结束本轮链路")
                        return RC_BUSY
                    log.info("   ↩ 交回上层重跑整段（exit 13）")
                    return RC_BUSY_RETRY

            if status == "gone":
                # 断线重连关掉后发现 War3 进程已经没了（手动结束任务 / 崩溃 / 秒退）：
                # 这一局彻底结束，大厅永远不会回来 → 回退到「房间开局」重新点开始游戏。
                #
                # 2026-11-27 16:31 用户定：**回不到 ④，或确认房间窗已经没了** →
                # 不再判失败，直接重新走 ①②③（搜图 → 建房 → 填密码），次数由
                # pipeline.json 的 rebuild_on_room_gone 控制，防死循环。
                def _rebuild_room(why_):
                    nonlocal idx, reopened, rebuild_left
                    if rebuild_left <= 0:
                        log.error(f"   ⛔ {why_}（重建房间次数已用完）→ 判本段失败")
                        return False
                    rebuild_left -= 1
                    log.warning(f"   ♻ {why_} → 从第 1 步「{steps[0].get('name')}」重新"
                                f"搜图建房（还剩 {rebuild_left} 次重建机会）")
                    BOUND_ROOM.clear()
                    PREFER_HWND["hwnd"] = None
                    idx = 0
                    reopened = True
                    return True

                if isinstance(err, RoomGone):
                    if _rebuild_room(f"{name}：断线弹窗收掉后房间窗已不存在"):
                        break
                    return 1
                try:
                    import room_finder as _rf_gone
                    _room_gone = _rf_gone.find_room(verbose=False)
                    if _room_gone and _rf_gone.is_room_busy(_room_gone):
                        log.error("   ⛔ 房间仍显示游戏中，停止重开并保留当前房间")
                        return 1
                except Exception as _e_gone:
                    log.error(f"   ⛔ 无法确认房间是否可开局：{_e_gone!r}；停止盲目重开")
                    return 1
                back = _reopen_target(steps, idx)
                if back is None:
                    # 回不到 ④（本次链路里没有「房间内开局」这一步）→ 只能重建房间
                    if _rebuild_room(f"{name}：没有可回退的开局步骤"):
                        break
                    return 1
                if reopen_left > 0:
                    reopen_left -= 1
                    log.warning(f"   ⛔ {name}：{err}")
                    log.warning(f"   ↻ 回退到第 {back + 1} 步「{steps[back].get('name')}」"
                                f"重新点「开始游戏」（还剩 {reopen_left} 次重开机会）")
                    idx = back
                    reopened = True
                    break
                log.error(f"   ⛔ {name}：{err}（重开次数已用完）")
                return 1
            if status == "failed":
                log.error(f"{name} 失败: {type(err).__name__}: {err}")
                # 2026-11-27 16:54 用户定：**④ 开局失败不再重启平台**，直接判本段失败，
                # 交给上层（full_loop）决定重跑整段还是结束本轮。平台重启只剩
                #「房间明确游戏中 / 退房无效」这类房间本身卡死的情况。
                return 1
            break

        if reopened:
            continue
        # ⚠️ for→while 改造时必须自己推进下标：少了这一句，第 1 步会 12 秒一轮
        # 无限重复（2026-11-18 23:14 实测：①→①→①…从 23:14 跑到 23:17 没出过第 2 步）。
        idx += 1
        done += 1
        log.info(f"━━ {name} 完成，用时 {time.time() - t0:.1f}s ━━")

        # 用户 2026-11-27 00:27 定：① 搜地图时弹的「您正在房间内…」
        # **不管房间在不在都点「确定」**（配置里第 5 步 / ④b 已经是这个口径），
        # 点完再显式走一遍退房流程，然后照原流程重建 —— 不做「点取消保房间」那套。
        # ① 只要真的跑过，就说明切过地图、房间状态已经脏了，退干净才不会连环弹。
        if "①" in name and st.get("task") == "search_double":
            try:
                import room_finder as _rf1
                if _rf1.find_room(verbose=False):
                    log.info("   ① 已切过地图 → 房间状态已脏，再走一遍退房流程")
                    _lr1 = _rf1.leave_room(verbose=True)
                    log.info(f"   退房结果：{_lr1}")
                    time.sleep(1.0)
            except Exception as _e1:
                log.info(f"   (① 后退房出错，按原流程继续：{type(_e1).__name__}: {_e1})")

        # 收尾检一次「您正在房间内…？」弹窗：它挡着下面所有点击。
        # 用户 2026-11-26 定：**点「确定」→ 退出房间 → 重建**
        #   · 房间没退干净（还能探到房间窗）→ 整段从第 1 步重来，别拿它开局
        #   · 详情页还在（认得到「创建房间」按钮）→ 跳过 ① 直接回 ② 重建房间
        #   · 详情页也丢了 → 整段从第 1 步重来
        # ①、② 都可能触发；先检查提示，再判断建房子弹窗是否出现。
        # room_finder 会跳过 584x517 建房子弹窗，避免把「创建」误点成「确定」。
        if st.get("check_warn"):
            try:
                import room_finder as _rf
                if _rf.handle_popup(prefer_cancel=False, scan_all=True, dry=True,
                                    verbose=False) == "found":
                    log.info("   ⚠️ 检测到「您正在房间内…」弹窗 → 点「确定」退出房间后重建")
                    _wr = _rf.warn_leave(verbose=True)
                    if _wr == "room_still":
                        log.info("   ⛔ 房间没退干净 → 整条链路从第 1 步（搜地图）重来")
                        return RC_NODLG
                    if _wr == "left":
                        _ok, _sc = _rf.has_create_room_btn(verbose=True)
                        _tgt = None
                        for _i, _st in enumerate(steps):
                            if _st.get("task") == "create_room":
                                _tgt = _i
                                break
                        if _ok and _tgt is not None:
                            log.info(f"   ↻ 详情页还在（「创建房间」score={_sc:.3f}）"
                                     f" → 跳过第 1 步，回到第 {_tgt + 1} 步"
                                     f"「{steps[_tgt].get('name')}」重建房间")
                            idx = _tgt
                            continue
                        log.info("   ⛔ 详情页也丢了（认不到「创建房间」按钮）"
                                 " → 整条链路从第 1 步重来")
                        return RC_NODLG
            except Exception:
                pass

        # ② 建房子：点完必须确认弹窗真的出来了，没出来当场补点，
        # 不把这个坑留给 ③（13:42 那一轮就是 ③ 绑到主窗盲打密码报废的）
        if "②" in name and not create_dialog_hwnd():
            log.info("   ⏳ 没看到建房子弹窗 → 确认「创建房间」是否点中")
            _oc = _open_create_dialog(base, tries=2)
            if _oc == "room_alive":
                _rh = ROOM_BLOCKED["hwnd"] or 0
                ROOM_BLOCKED["hwnd"] = 0
                if _rh:
                    # 把这扇「盖在主窗上的房间窗」直接登记好，④ 绑窗时优先用它
                    # （W7 上房间画面可能还没刷新，模板认不出，不能靠 find_room 再找一遍）
                    BOUND_ROOM.update({"hwnd": _rh, "via": "room_alive", "scores": None})
                    PREFER_HWND["hwnd"] = _rh
                _back = next((k for k, s2 in enumerate(steps) if s2.get("activate_room")), None)
                if _back is not None:
                    # 复用旧房间前必须校验房间名/密码（否则 ③ 填密码会被整步跳过）
                    _v = _reuse_room_check()
                    if _v == "ok":
                        log.info(f"   ⏭ 房间窗其实还在（就压在主窗上）→ 不建房，直接进第 "
                                 f"{_back + 1} 步「{steps[_back].get('name')}」开局")
                        idx = _back
                        continue
                    if _v == "login":
                        return RC_LOGIN
                    if _v == "platform_restarted":
                        return RC_RESTARTED
                    if _v == "game_running":
                        return 0
                    _reuse_bad += 1
                    if _reuse_bad > 2:
                        log.error(f"   ⛔ 复用的房间连续 {_reuse_bad} 次不合格（{_v}）→ 停止本轮")
                        return RC_NODLG
                    log.warning(f"   ⚠️ 复用的房间不合格（{_v}）→ 已退房，回第 "
                                f"{_back_to_fill_pwd() + 1} 步重新填密码建房"
                                f"（第 {_reuse_bad}/2 次）")
                    BOUND_ROOM.update({"hwnd": None, "via": None, "scores": None})
                    PREFER_HWND["hwnd"] = None
                    idx = _back_to_fill_pwd()
                    continue
                log.error("   ⛔ 房间还在，但 pipeline 里没有「房间内开局」那一步 → 无法复用")
                return RC_NODLG
            if not _oc:
                log.error("   ⛔ 连点 3 次「创建房间」，建房子弹窗始终没出来 → 整条链路从第 1 步重跑")
                return RC_NODLG

        settle = float(st.get("settle", 0) or 0) / 1000.0
        # ③ 点完「创建」→ 先静默 6s（平台销毁弹窗 + 重建房间窗），再探房间窗并抓到前台。
        # 探到就不再走 settle 死等；真等不到才退回原来那个 settle 兜底。
        nxt = steps[idx] if idx < len(steps) else None
        if nxt and nxt.get("activate_room"):
            log.info(f"   先静默等 {ROOM_FRONT_DELAY:.0f}s（平台销毁弹窗 + 重建房间窗），再识别房间窗")
            time.sleep(ROOM_FRONT_DELAY)
            _room, _waited = await_room_front()
            if _room:
                log.info(f"   \U0001F5A5 房间窗已到前台 hwnd={_room['hwnd']} "
                         f"{_room['client'][2]}x{_room['client'][3]}"
                         f"（固定 {ROOM_FRONT_DELAY:.0f}s + 探测 {_waited:.1f}s，不再空等 {settle:.1f}s）")
                settle = 0.0
            else:
                log.info(f"   \u23f3 固定 {ROOM_FRONT_DELAY:.0f}s + 探测 {_waited:.1f}s 还没看到房间窗 "
                         f"→ 按原方式等 {settle:.1f}s 兜底")
        if settle:
            log.info(f"   等 {settle:.1f}s 让下一个窗口出来")
            time.sleep(settle)

    log.info(f"✅ 全链路完成（{done} 步），总用时 {time.time() - t_all:.1f}s")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="KK官方对战平台 游戏自动化引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list-windows", help="列出可用窗口")

    pp = sub.add_parser("pipeline", help="一条命令跑完整链路（搜地图→建房→填密码→开局→选难度开打）")
    pp.add_argument("--pipeline", default="configs/pipeline.json")
    pp.add_argument("--only", default=None,
                    help="只跑名字里含该关键字的那一步；多个用逗号，如 --only ④,⑤（调试）")
    pp.add_argument("--no-room-check", action="store_true",
                    help="跳过开局前的房间校验（房间名「我在峡谷打」/ 密码是否已设）")
    pp.add_argument("--no-map-update", action="store_true",
                    help="不处理「地图已更新，请重新下载地图」弹窗（默认命中就关提示 + 重建房间）")
    pp.add_argument("--busy-wait", type=float, default=None, metavar="MIN",
                    help="遇到「您正在游戏中，不能创建游戏房间」时点确定后等多少分钟，"
                         "然后整段从第 1 步重来（默认取 pipeline.json 的 busy_wait_minutes，缺省 6）")
    pp.add_argument("--busy-retries", type=int, default=None, metavar="N",
                    help="（已废弃，保留兼容）尝试次数现在由 full_loop 的 busy_attempts 控制")
    pp.add_argument("--no-busy-wait", action="store_true",
                    help="不等，遇到就直接报错退出（调试用）")
    pp.add_argument("--busy-restart", dest="busy_restart", action="store_true", default=None,
                    help="等完仍不释放时自动重启平台（默认开，界面可关）")
    pp.add_argument("--no-busy-restart", dest="busy_restart", action="store_false",
                    help="不自动重启平台，只提示手动处理")
    pp.add_argument("--dry-run", action="store_true")

    for name, fn in (("run", cmd_run), ("shot", cmd_shot),
                     ("crop", cmd_crop), ("probe", cmd_probe)):
        p = sub.add_parser(name)
        p.add_argument("--config", default="configs/arcade.json")
        p.add_argument("--task", default=None)
        p.add_argument("--autostart", action="store_true", help="不等热键，直接开跑")
        p.add_argument("--dry-run", action="store_true",
                       help="只跑流程不发按键（安全检查配置用）")
        p.add_argument("--shot", default="shot.png")
        p.add_argument("--crop", default="0,0,100,100")
        p.add_argument("--out", default="templates/new.png")
        p.add_argument("--probe", default="10,10")
        p.add_argument("--window-title", default=None)
        p.add_argument("--window-exe", default=None)
        p.add_argument("--capture", default="auto",
                       choices=["auto", "printwindow", "screen"])

    # 兼容直接传 --config 而不写子命令
    argv = sys.argv[1:]
    if argv and argv[0].startswith("--") and argv[0] not in ("--help", "-h"):
        argv = ["run"] + argv
    args = ap.parse_args(argv)

    if args.cmd in (None,):
        ap.print_help()
        return 0
    if args.cmd == "list-windows":
        cmd_list_windows()
        return 0
    if args.cmd == "pipeline":
        return cmd_pipeline(args)
    fn = {"run": cmd_run, "shot": cmd_shot,
          "crop": cmd_crop, "probe": cmd_probe}[args.cmd]
    return fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
