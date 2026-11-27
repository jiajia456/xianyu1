# -*- coding: utf-8 -*-
"""平台重启守卫 —— 僵尸会话等不回来时，自动关掉平台并重新打开。

背景（用户 2026-11-17）：
    强杀 War3 之后，平台进程里那一局还挂着，建房时弹
    「平台提示 / 您正在游戏中，不能创建游戏房间」。
    点「确定」+ 等 N 分钟 × 最多 3 次尝试（默认 10 分钟 × 3）还是不放行，
    说明会话真的卡死在 Platform.exe 里 —— 只能重启平台。

本模块做三件事：
    1. info()      找到平台主窗口（480x300 的弹窗不算，标题虽然一样）
    2. restart()   记下 exe 路径 → 关窗/强杀 → 重新启动 → 等大厅（搜索框）加载好
    3. wait_ready()等待期间一旦弹「登录 / 顶号」立刻收手，不自动登录

对外返回统一用 dict：
    {"status": "ok" | "login" | "war3" | "noexe" | "fail", ...}

CLI 自检：
    python plat_restart.py --info       # 看看平台窗口 / 进程路径
    python plat_restart.py --restart    # 真重启一次（会关掉当前平台！）
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_LOBBY = os.path.join(HERE, "templates", "search_box.png")
EXE_NAME = "platform.exe"
WAR3_NAMES = ("war3.exe", "warcraft iii.exe", "y3.exe")
# 平台装在哪：优先从进程里读，读不到再按这些常见目录兜底
GUESS_DIRS = (r"D:\kkduizhan", r"E:\kkduizhan", r"C:\kkduizhan",
              r"D:\KK对战平台", r"E:\KK对战平台")
WM_CLOSE = 0x0010
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_ESCAPE = 0x1B
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_VERBOSE = True


def say(m=""):
    if _VERBOSE:
        print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# ------------------------------------------------------------------ 进程/窗口
def _exe_of(pid: int) -> str:
    """取进程完整路径（拿不到返回空串，权限不足很常见）"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value or ""
            return ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def _all_pids():
    try:
        import win32process
        return list(win32process.EnumProcesses())
    except Exception:
        return []


def _procs(names=(EXE_NAME,)):
    """返回 [(pid, path)]：匹配给定 exe 名的进程"""
    want = {n.lower() for n in names}
    out = []
    for pid in _all_pids():
        p = _exe_of(pid)
        if p and os.path.basename(p).lower() in want:
            out.append((int(pid), p))
    return out


def war3_alive():
    """War3 还活着吗（真在打游戏时绝不重启平台，会把这一局连同存档一起弄没）"""
    return bool(_procs(WAR3_NAMES))


def _main_windows():
    """平台主窗口（>=600x400；480x300 的弹窗标题一样，必须按尺寸排掉）"""
    import win32gui

    out = []

    def cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
                return
            title = win32gui.GetWindowText(h) or ""
        except Exception:
            return
        low = title.lower()
        if "对战平台" not in title and not low.startswith("kk"):
            return
        if "登录" in title:            # 登录页也带「KK官方对战平台」字样，但不是大厅
            return
        try:
            r = win32gui.GetClientRect(h)
        except Exception:
            return
        if r[2] < 600 or r[3] < 400:
            return
        try:
            import win32process
            pid = win32process.GetWindowThreadProcessId(h)[1]
        except Exception:
            pid = 0
        out.append(dict(hwnd=h, title=title, w=int(r[2]), h=int(r[3]), pid=int(pid)))

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda x: -(x["w"] * x["h"]))
    return out


def _win_normal_size(h):
    """窗口的「正常尺寸」。最小化时 GetClientRect / GetWindowRect 都是垃圾值
    （Win7 上 client 直接 0x0），只有 GetWindowPlacement 的 rcNormalPosition 靠得住。"""
    import win32gui
    try:
        plc = win32gui.GetWindowPlacement(h)
        r = plc[4]                       # rcNormalPosition
        return max(0, int(r[2]) - int(r[0])), max(0, int(r[3]) - int(r[1]))
    except Exception:
        pass
    try:
        r = win32gui.GetWindowRect(h)
        return max(0, int(r[2]) - int(r[0])), max(0, int(r[3]) - int(r[1]))
    except Exception:
        return 0, 0


def restore_minimized(verbose=True):
    """平台主窗是最小化时把它恢复出来，返回恢复的窗口数。

    2026-11-27 17:27 现场：用户把 KK 最小化后启动脚本 → _main_windows() 按
    `IsIconic → return` 排掉最小化窗 → info() 拿不到 hwnd → ensure_running 当成
    「平台没开」→ wait_ready 白等 30 秒 → nowin → 整条链路起不来。
    最小化不是「没开」，先恢复再判定。
    """
    import win32gui
    import win32con
    n = 0

    def cb(h, _):
        nonlocal n
        try:
            if not win32gui.IsWindowVisible(h) or not win32gui.IsIconic(h):
                return
            title = win32gui.GetWindowText(h) or ""
        except Exception:
            return
        low = title.lower()
        if "对战平台" not in title and not low.startswith("kk"):
            return
        w, hh = _win_normal_size(h)
        if w < 600 or hh < 400:          # 480x300 弹窗、悬浮小窗不算主窗
            return
        try:
            win32gui.ShowWindow(h, win32con.SW_RESTORE)
            n += 1
        except Exception:
            pass

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return 0
    if n:
        time.sleep(0.8)
        if verbose:
            say(f"   ✓ 平台主窗原本最小化 → 已恢复（{n} 扇）")
    return n


def info():
    """当前平台状态：窗口 + 进程 + exe 路径（都没有返回 None）"""
    ws = _main_windows()
    ps = _procs()
    if not ws and not ps:
        return None
    d = dict(ws[0]) if ws else dict(hwnd=0, title="", w=0, h=0, pid=ps[0][0])
    if not d.get("pid") and ps:
        d["pid"] = ps[0][0]
    d["pids"] = [p for p, _ in ps]
    exe = ""
    for pid, p in ps:
        if p:
            exe = p
            break
    if not exe and d.get("pid"):
        exe = _exe_of(d["pid"])
    d["exe"] = exe
    d["war3"] = war3_alive()
    return d


def guess_exe():
    """平台 exe 路径：进程、常见安装目录、桌面快捷方式依次查找。"""
    d = info()
    if d and d.get("exe") and os.path.exists(d["exe"]):
        return d["exe"]
    for base in GUESS_DIRS:
        p = os.path.join(base, "Platform.exe")
        if os.path.exists(p):
            return p
    # 用户桌面已有 KK 快捷方式时，安装位置可能不在默认盘符。
    script = ("$w=New-Object -ComObject WScript.Shell; "
              "$dirs=@([Environment]::GetFolderPath('Desktop'),"
              "[Environment]::GetFolderPath('CommonDesktopDirectory')); "
              "foreach($d in $dirs){if(Test-Path $d){"
              "Get-ChildItem -LiteralPath $d -Filter '*.lnk' | ForEach-Object {"
              "$t=$w.CreateShortcut($_.FullName).TargetPath; "
              "if([IO.Path]::GetFileName($t) -ieq 'Platform.exe' -and "
              "(Test-Path -LiteralPath $t)){Write-Output $t; break}}}}")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, encoding="mbcs", timeout=12)
        for line in r.stdout.splitlines():
            p = line.strip()
            if os.path.isfile(p) and os.path.basename(p).lower() == "platform.exe":
                return p
    except Exception:
        pass
    return ""


def ensure_running(verbose=True):
    """脚本启动时确保 KK 平台已打开；已运行则直接复用。"""
    global _VERBOSE
    _VERBOSE = verbose
    d = info()
    if d and d.get("hwnd"):
        say("   ✓ KK 平台主窗口已打开")
        return dict(status="ok", existing=True, exe=d.get("exe", ""))
    # 2026-11-27 17:27：进程在、但主窗被最小化 → 先恢复，别当成「平台没开」
    if d and not d.get("hwnd"):
        if restore_minimized(verbose=verbose):
            d = info()
            if d and d.get("hwnd"):
                say("   ✓ KK 平台主窗口已恢复（之前是最小化）")
                return dict(status="ok", existing=True, restored=True,
                            exe=d.get("exe", ""))
    exe = (d or {}).get("exe") or guess_exe()
    if not exe or not os.path.exists(exe):
        say("   ⛔ 未找到 KK 平台安装路径；请先安装或手动启动一次 KK")
        return dict(status="noexe")
    if not d:
        # 与另一个启动入口同时运行时，再查一次，避免重复拉起两个 KK 实例。
        time.sleep(1)
        d = info()
        if not d and not start(exe, verbose=verbose):
            return dict(status="fail", why="start_failed", exe=exe)
    ready = wait_ready(verbose=verbose)
    ready["exe"] = exe
    return ready


# ------------------------------------------------------------------ 日志
def _log_event(kind, **kw):
    try:
        d = os.path.join(HERE, "logs")
        os.makedirs(d, exist_ok=True)
        rec = dict(ts=time.strftime("%Y-%m-%d %H:%M:%S"), epoch=int(time.time()), kind=kind)
        rec.update(kw)
        with open(os.path.join(d, "platform_restart.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ 关 / 开
def _post_close(hwnd):
    try:
        import win32gui
        win32gui.PostMessage(hwnd, WM_CLOSE, 0, 0)
        return True
    except Exception:
        return False


def _wait_procs_gone(timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not _procs():
            return True
        time.sleep(0.5)
    return not _procs()


_TPL_LEAVE_TXT = os.path.join(HERE, "templates", "lbl_leave_confirm.png")
_TPL_OK_BLUE = os.path.join(HERE, "templates", "btn_platform_confirm.png")


def _click_exit_confirm(verbose=True, warned=None):
    """关平台时冒出确认框就点「确定」，让它体面退出（保住登录态）。

    2026-11-17 13:43 实测：房间里关平台，弹的是
    「平台提示 / 是否确认退出房间？/ [确定] 取消(N)」，老的 btn_popup_ok/yes 模板
    一个都不中（0.408）→ 连刷 9 条「未知弹窗」后只能强杀。
    现在认 lbl_leave_confirm.png（真弹窗 1.000）定位文字，
    再用 btn_platform_confirm.png（0.994）点那颗蓝「确定」。
    认不出来的窗口只存图，同一个窗口只提示一次（不再刷屏）。
    """
    n = 0
    try:
        import popup_guard as pg
    except Exception:
        return 0
    warned = warned if warned is not None else set()
    tpls = [t for t in (os.path.join(HERE, "templates", f)
                        for f in ("btn_popup_ok.png", "btn_popup_yes.png"))
            if os.path.exists(t)]
    for w in _popup_windows():
        img = pg.grab_hwnd(w["hwnd"])
        if img is None or not getattr(img, "size", 0):
            continue
        # ① 「是否确认退出房间？」→ 点蓝「确定」（退出房间后主窗才能关）
        leave = pg.match(img, _TPL_LEAVE_TXT, 0.85) if os.path.exists(_TPL_LEAVE_TXT) else None
        if leave and leave[0] >= 0.85:
            ok = pg.match(img, _TPL_OK_BLUE, 0.85) if os.path.exists(_TPL_OK_BLUE) else None
            if ok and ok[0] >= 0.85:
                try:
                    pg.click_screen(w["hwnd"], ok[1][0], ok[1][1])
                    n += 1
                    if verbose:
                        say(f"      ✓ 「是否确认退出房间？」点了确定"
                            f"（文字 {leave[0]:.3f} / 按钮 {ok[0]:.3f} @{ok[1][0]},{ok[1][1]}）")
                except Exception as e:
                    if verbose:
                        say(f"      ⚠️ 点退出确认框失败：{e!r}")
                continue
        # ② 老确认框模板
        hit = None
        for t in tpls:
            r = pg.match(img, t, 0.85)
            if r and r[0] >= 0.85 and (hit is None or r[0] > hit[0]):
                hit = (r[0], r[1], t)
        if hit is not None:
            score, (cx, cy), tpl = hit
            try:
                pg.click_screen(w["hwnd"], cx, cy)
                n += 1
                if verbose:
                    say(f"      ✓ 退出确认框点了「{os.path.basename(tpl)}」"
                        f"（score={score:.3f} @{cx},{cy}）")
            except Exception as e:
                if verbose:
                    say(f"      ⚠️ 点退出确认框失败：{e!r}")
            continue
        # ③ 认不出来 → 只存图，同一个窗口只吼一次
        if w["hwnd"] in warned:
            continue
        warned.add(w["hwnd"])
        try:
            p = os.path.join(HERE, "logs", "exit_confirm_unknown.png")
            import cv2
            cv2.imencode(".png", img)[1].tofile(p)
            if verbose:
                say(f"      ❔ 退出时出现未知弹窗 {w['w']}x{w['h']} → {p}（没敢点）")
        except Exception:
            pass
    return n


def _platform_family(exe):
    """同一 KK 安装目录内的程序；排除游戏与本自动化程序。"""
    root = os.path.normcase(os.path.abspath(os.path.dirname(exe))) + os.sep
    own = os.getpid()
    out = []
    for pid in _all_pids():
        if not pid or pid == own:
            continue
        path = _exe_of(pid)
        if not path or not os.path.normcase(os.path.abspath(path)).startswith(root):
            continue
        if os.path.basename(path).lower() in WAR3_NAMES:
            continue
        out.append((int(pid), path))
    return out


def close(wait_close=8.0, wait_kill=12.0, verbose=True, exe=None) -> bool:
    """结束 Platform.exe 及同一 KK 安装目录内的子程序，并确认全部退出。"""
    if verbose:
        say("   ① 关闭 KK 平台及其子进程")
    left = list(_procs())
    if exe:
        left.extend(_platform_family(exe))
    for window in _main_windows():
        if window.get("pid"):
            left.append((window["pid"], exe or "Platform.exe"))
    left = list({pid: (pid, path) for pid, path in left}.values())
    if verbose and left:
        say(f"      进程：{', '.join(str(p) for p, _ in left)}")
    for pid, _ in left:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=20)
        except Exception:
            pass
    end = time.monotonic() + wait_kill
    while time.monotonic() < end:
        if not _procs() and not _main_windows() and (not exe or not _platform_family(exe)):
            break
        time.sleep(0.5)
    ok = not _procs() and not _main_windows() and (not exe or not _platform_family(exe))
    if verbose:
        say("      ✅ 进程已清空" if ok else "      ❌ 进程仍在，杀不干净")
    return ok


def start(exe=None, verbose=True):
    """启动平台，返回是否成功起来了"""
    exe = exe or guess_exe()
    if not exe or not os.path.exists(exe):
        if verbose:
            say(f"      ❌ 找不到 Platform.exe（{exe or '路径未知'}）")
        return False
    if verbose:
        say(f"   ② 重新启动：{exe}")
    # 优先用 ShellExecute（os.startfile）：平台完全脱离自动化进程树，
    # 自动化那段脚本结束/被掐也不会把平台带走（2026-11-17 实测 Popen 会被一起收掉）
    try:
        os.startfile(exe)          # noqa: S606 - 就是这个用途
        return True
    except Exception as e:
        if verbose:
            say(f"      (ShellExecute 失败 {e!r}，改用 Popen)")
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        return True
    except Exception as e:
        if verbose:
            say(f"      ❌ 启动失败：{e!r}")
        return False


def _popup_windows():
    """480×300 量级的 KK 顶层弹窗（主窗不算）"""
    import win32gui

    out = []

    def cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
                return
            title = win32gui.GetWindowText(h) or ""
        except Exception:
            return
        low = title.lower()
        if "对战平台" not in title and not low.startswith("kk"):
            return
        try:
            r = win32gui.GetClientRect(h)
        except Exception:
            return
        w, hh = int(r[2]), int(r[3])
        if not (200 <= w <= 1000 and 150 <= hh <= 700):
            return
        if w >= 600 and hh >= 400:        # 主窗
            return
        out.append(dict(hwnd=h, w=w, h=hh, title=title))

    win32gui.EnumWindows(cb, None)
    return out


def esc_popups(verbose=True, keep=(), skip_once=None):
    """给平台的弹窗按 ESC 关掉（用户 2026-11-17：重启平台过程中弹的东西可以按 ESC 屏蔽）。

    `keep` 里的窗口不动（比如正在检测的「您正在游戏中」）。
    `skip_once` 传一个 set 进来，用来记住已经按过的窗口，避免每 2 秒狂按。
    """
    import win32gui

    n = 0
    for w in _popup_windows():
        if w["hwnd"] in keep:
            continue
        if skip_once is not None:
            if w["hwnd"] in skip_once:
                continue
            skip_once.add(w["hwnd"])
        try:
            win32gui.PostMessage(w["hwnd"], WM_KEYDOWN, VK_ESCAPE, 0)
            win32gui.PostMessage(w["hwnd"], WM_KEYUP, VK_ESCAPE, 0)
            n += 1
            if verbose:
                say(f"      ⎋ 弹窗 {w['w']}x{w['h']} {w['title'][:16]!r} → 按 ESC 关掉")
        except Exception:
            pass
    return n


def _set_grace(sec=180.0):
    """写「重启宽限期」：这段时间内各进程的登录守卫都不判登录窗。

    2026-11-27 用户定：自动重启平台后，登录窗要复查 3 次 × 30 秒，都还在才停手。
    """
    try:
        import login_guard as lg
        os.makedirs(lg.LOGS, exist_ok=True)
        with open(lg.GRACE_PATH, "w", encoding="utf-8") as f:
            json.dump({"until": time.time() + sec if sec else 0}, f)
    except Exception:
        pass


def _login_hit(persist=2.5):
    """重启后有没有登录/顶号窗口。

    先纯检测（scan，不写日志），2.5 秒后再看一次：只有**持续存在**才算命中。
    这样平台启动瞬间闪一下的登录框（随后自动登录）不会把整轮任务误停。
    复查时必须 ignore_grace=True —— 否则宽限期把 scan 挡掉，永远看不到登录窗。
    """
    try:
        import login_guard as lg
    except Exception:
        return None
    try:
        if not lg.scan(ignore_grace=True):
            return None
        time.sleep(persist)
        if not lg.scan(ignore_grace=True):
            return None
        return lg.check("平台重启后", quiet=False, dedup=0)
    except Exception:
        return None


def _lobby_ready(hwnd):
    """主窗里能看到搜索框 → 大厅加载完了"""
    try:
        import popup_guard as pg
        if not os.path.exists(TPL_LOBBY):
            return None
        img = pg.grab_hwnd(hwnd)
        r = pg.match(img, TPL_LOBBY, 0.85)
        if r:
            return float(r[0])
        return 0.0
    except Exception:
        return None


def _keep_hwnds():
    """不能按 ESC 的窗口：正在检测的「您正在游戏中」弹窗（ESC 掉就丢信号了）"""
    keep = set()
    try:
        import busy_guard as _bg
        hb = _bg.find(verbose=False)
        if hb:
            keep.add(hb["hwnd"])
    except Exception:
        pass
    return keep


def wait_ready(win_timeout=150.0, lobby_timeout=120.0, verbose=True):
    """等主窗出现 + 大厅可用。返回 dict(status/…)

    status: ok / login / nowin

    2026-11-20 16:44 用户要求：平台重启流程**零干扰**——
      打开后静默 5 秒（不检测/不操作）→ 纯枚举等主窗（30 秒，零抓图零按键）
      → 主窗出现后固定等 20 秒当大厅就绪（纯时间判断，不识别/不点广告）。
    """
    if verbose:
        say("   ③ 启动后静默 5 秒（不做任何检测/操作）")
    time.sleep(5)

    t0 = time.time()
    win = None
    while time.time() - t0 < 30:
        ws = _main_windows()          # 纯 EnumWindows + GetWindowText，无抓图
        if ws:
            win = ws[0]
            if verbose:
                say(f"   ③ 平台窗口已出现（{win['w']}x{win['h']}，"
                    f"用时 {time.time() - t0:.1f}s）")
            break
        time.sleep(0.5)
    if not win:
        # 30 秒没进主窗 → 区分「停在登录窗（要人工登录）」和「窗口没起来」
        hit = _login_hit()
        if hit:
            if verbose:
                say(f"      ⛔ 30s 没进主窗、停在登录窗口（{hit.get('ts')}）→ 需要人工登录")
            return dict(status="login", detail=hit, seconds=int(time.time() - t0))
        if verbose:
            say("      ❌ 30s 内没等到平台窗口")
        return dict(status="nowin", seconds=int(time.time() - t0))

    # 主窗出现 → 固定等 20 秒（纯时间判断，不识别/不操作）
    if verbose:
        say("   ④ 主窗已出现 → 固定等 20 秒（纯时间判断，不做任何识别）")
    time.sleep(20)
    # 2026-11-27 用户定：重启后登录窗给 3 次 × 30 秒宽限（平台可能自己登回去）。
    # 3 次复查都还在登录窗才判 login（上层停手）；中途消失就照常继续。
    for _i in range(1, 4):
        time.sleep(30)
        try:
            import login_guard as _lg
            _hits = _lg.scan(ignore_grace=True)
        except Exception:
            _hits = []
        if not _hits:
            if verbose:
                say(f"   ④ 第 {_i}/3 次复查：没有登录窗 → 视为就绪"
                    f"（总用时 {time.time() - t0:.0f}s）")
            return dict(status="ok", lobby="assumed", checked=_i,
                        seconds=int(time.time() - t0))
        if verbose:
            say(f"   ④ 第 {_i}/3 次复查：登录窗还在 → 等 30 秒再看")
    _set_grace(0)                     # 复查结束，先撤宽限，才记事件/交给上层停手
    hit = _login_hit()
    if hit:
        return dict(status="login", detail=hit, seconds=int(time.time() - t0))
    if verbose:
        say(f"   ④ 大厅视为就绪（总用时 {time.time() - t0:.0f}s）")
    return dict(status="ok", lobby="assumed", seconds=int(time.time() - t0))


def dismiss_disc(verbose=True):
    """平台重启后清理残留断线提示，避免再次触发重启。"""
    try:
        import disc_guard as dg
    except Exception:
        return None
    try:
        if not (dg.find() or dg.find_fail()):
            return None
        if verbose:
            say("      ↪ 重启后仍有断线提示，关掉残留弹窗")
        a = dg.dismiss(verbose=verbose)
        b = dg.dismiss_fail(verbose=verbose)
        return dict(result=f"{a}/{b}")
    except Exception as e:
        if verbose:
            say(f"      (断线重连收尾出错：{e!r})")
        return dict(result=f"error:{e!r}")


def restart(exe=None, verbose=True, close_timeout=12.0, win_timeout=150.0,
            lobby_timeout=120.0, force=False):
    """完整重启：关 → 开 → 等大厅。返回 dict(status, …)

    status:
      ok      → 平台已重开，可以重试建房（lobby=True 表示确认看到大厅）
      login   → 重开后要登录/被顶号 → 上层必须停手（不自动登录）
      war3    → War3 还在跑，不敢重启（会把这一局弄没）
      noexe   → 找不到 Platform.exe
      fail    → 关闭或启动失败
    """
    global _VERBOSE
    _VERBOSE = verbose
    t0 = time.time()
    d = info()
    if d and d.get("war3") and not force:
        say("   ⛔ War3 还在运行 → 不重启平台（会把这一局弄没）")
        _log_event("restart_skip", why="war3_running")
        return dict(status="war3", seconds=0)
    exe = exe or (d or {}).get("exe") or guess_exe()
    if not exe or not os.path.exists(exe):
        say(f"   ❌ 找不到 Platform.exe（{exe or '未知'}）→ 无法自动重启")
        _log_event("restart_fail", why="noexe", exe=exe)
        return dict(status="noexe", exe=exe, seconds=0)

    say(f"   ♻️ 自动重启平台（{exe}）")
    if not close(close_timeout, verbose=verbose, exe=exe):
        _log_event("restart_fail", why="close_failed", exe=exe)
        return dict(status="fail", why="close_failed", exe=exe,
                    seconds=int(time.time() - t0))
    # 2026-11-20 16:44 用户要求：关闭后静默等 8 秒再打开（不做任何检测/操作）
    say("   ①b 关闭后静默 8 秒（不做任何检测/操作）")
    time.sleep(8)
    # 2026-11-27：宽限要覆盖「启动 5s + 等主窗 ≤30s + 固定 20s + 3×30s 复查」≈145s，
    # 这段时间各进程的登录守卫都不判登录窗（由 wait_ready 自己复查）。
    _set_grace(180)
    if not start(exe, verbose=verbose):
        _set_grace(0)
        _log_event("restart_fail", why="start_failed", exe=exe)
        return dict(status="fail", why="start_failed", exe=exe,
                    seconds=int(time.time() - t0))

    try:
        r = wait_ready(win_timeout, lobby_timeout, verbose=verbose)
    finally:
        _set_grace(0)
    r["exe"] = exe
    if r.get("status") == "ok":
        r["disc"] = dismiss_disc(verbose=verbose)
    r["seconds"] = int(time.time() - t0)
    _log_event("restart_" + r.get("status", "?"), exe=exe, lobby=r.get("lobby"),
               score=r.get("score"), seconds=r["seconds"])
    return r


# ------------------------------------------------------------------ CLI
def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="平台重启守卫")
    ap.add_argument("--info", action="store_true", help="只看平台窗口/进程路径")
    ap.add_argument("--ensure", action="store_true", help="KK 未启动时自动打开并等待大厅")
    ap.add_argument("--restart", action="store_true", help="真重启平台（会关掉当前平台）")
    ap.add_argument("--yes", action="store_true", help="不确认，直接干")
    a = ap.parse_args(argv)

    if a.ensure:
        r = ensure_running(verbose=True)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("status") == "ok" else 1

    if a.info or not (a.restart):
        d = info()
        if not d:
            print("❌ 没找到平台窗口/进程（平台没开？）")
            return 1
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    if not a.yes:
        print("⚠️ 这会关掉 KK 官方对战平台并重新打开。加 --yes 确认执行。")
        return 2
    r = restart(verbose=True)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
