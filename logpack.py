# -*- coding: utf-8 -*-
"""日志打包（GUI 按钮 / 查看日志.exe 共用）

用户 2026-11-18 16:40：「成品设置一个按钮开关，我点击一下就能获取日志方便复制给你」。

所以这里只做一件事：把「日志 + 环境信息」收进一个文件夹，再压成**一个 zip**，
zip 放在桌面 —— 用户把 zip 拖进聊天窗口就能发过来，不用一层层找目录。

与 kk_log_tool.py 的关系：本模块是唯一实现，kk_log_tool.py 只是它的命令行外壳
（打包成「查看日志.exe」）。GUI 里点按钮走 `pack()`，不弹窗、不开新进程。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import glob
import os
import shutil
import sys
import time
import zipfile

APP = "KK日志"

# 收集哪些扩展名 + 每个来源最多取多少个（先按修改时间倒序）
PATTERNS = ("*.log", "*.jsonl", "*.json", "*.txt")
PER_SOURCE_LIMIT = 200
# 截图也收（只收最新的几张）：换电脑后「等不到按钮 / 点不中」全靠它定位，
# 但历史截图能堆到几十 MB，所以按张数 + 总字节双限（2026-11-18 用户第一包日志里
# 一张图都没有，只能靠猜窗口尺寸）。
SHOT_DIRS = ("miss", "shot")
SHOT_LIMIT = 12
SHOT_MAX_BYTES = 12 * 1024 * 1024
# 调试现场（logs\debug）单独一份预算：「抓现场」一次就有几十张截图 + 打分 json，
# 塞进上面那 12 张的额度里会被挤掉，所以单列一份（2026-11-25）。
DBG_LIMIT = 120
DBG_MAX_BYTES = 40 * 1024 * 1024
LOG_DIR_NAMES = ("logs", os.path.join("core", "logs"))


# ---------------------------------------------------------------- 系统信息
def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _shget(csidl: int) -> str:
    buf = ctypes.create_unicode_buffer(260)
    try:
        ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    except Exception:
        return ""
    return buf.value


def desktop_dir() -> str:
    d = _shget(0x0010)          # CSIDL_DESKTOPDIRECTORY
    if d and os.path.isdir(d):
        return d
    return os.path.expanduser("~")


def screen_info() -> str:
    u = ctypes.windll.user32
    try:
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        mon = u.GetSystemMetrics(80)
    except Exception:
        sw = sh = vw = vh = mon = 0
    dpi = 96
    try:
        hdc = u.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        u.ReleaseDC(0, hdc)
    except Exception:
        pass
    try:
        ver = sys.getwindowsversion()
        winver = f"{ver.major}.{ver.minor} build {ver.build}"
    except Exception:
        winver = "?"
    lines = [
        f"Windows      : {winver}",
        f"主屏         : {sw} x {sh}",
        f"虚拟屏       : {vw} x {vh}   显示器数 {mon}",
        f"DPI          : {dpi}  (缩放 {dpi / 96.0 * 100:.0f}%)",
        "",
        ">> 选难度是按 1690x921 基准等比换算的；把 War3 客户区尺寸和上面的 DPI 发来，",
        "   就能判断是换算比例问题还是识别判据问题。",
    ]
    return "\n".join(lines)


def enum_windows():
    """所有可见顶层窗：标题 / 客户区 / 窗口矩形。"""
    u = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        try:
            if not u.IsWindowVisible(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if not title:
                return True
            cr = wt.RECT()
            u.GetClientRect(hwnd, ctypes.byref(cr))
            wr = wt.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(wr))
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cls, 256)
            out.append({
                "title": title,
                "class": cls.value,
                "cw": cr.right - cr.left,
                "ch": cr.bottom - cr.top,
                "ww": wr.right - wr.left,
                "wh": wr.bottom - wr.top,
                "min": bool(u.IsIconic(hwnd)),
            })
        except Exception:
            pass
        return True

    u.EnumWindows(cb, 0)
    return out


KEY_TITLE = ("war3", "warcraft", "kk", "对战", "魔兽", "y3", "僵尸", "峡谷", "少女梦")
KEY_CLASS = ("warcraft", "oswindow", "war3")


def is_key(w) -> bool:
    t = (w["title"] or "").lower()
    c = (w["class"] or "").lower()
    return any(k in t for k in KEY_TITLE) or any(k in c for k in KEY_CLASS)


def windows_report() -> str:
    ws = enum_windows()
    key = [w for w in ws if is_key(w)]
    rest = [w for w in ws if not is_key(w)]
    lines = ["== 关键窗口（War3 / 平台）=="]
    if not key:
        lines.append("  （没找到 War3 或平台窗口 —— 游戏/平台可能没开）")
    for w in key:
        flag = " [最小化]" if w["min"] else ""
        note = ""
        if w["cw"] <= 0 or w["ch"] <= 0:
            note = "  <- 客户区为 0，抓不到画面（窗口被最小化/隐藏，识别必失败）"
        lines.append(f"  {w['title']!r}  客户区 {w['cw']}x{w['ch']}  "
                     f"窗口 {w['ww']}x{w['wh']}  类 {w['class']!r}{flag}{note}")
    lines.append("")
    lines.append(f"== 其他可见窗口（共 {len(rest)} 个）==")
    for w in rest[:25]:
        lines.append(f"  {w['title'][:60]!r}  客户区 {w['cw']}x{w['ch']}")
    if len(rest) > 25:
        lines.append(f"  ... 还有 {len(rest) - 25} 个")
    return "\n".join(lines)


# ---------------------------------------------------------------- 找日志
def exe_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def candidate_roots():
    """可能是「成品根」或「源码根」的目录，按优先级。"""
    here = exe_dir()
    roots = [here]
    up = os.path.dirname(here)
    if up and up not in roots:
        roots.append(up)
    for base in list(roots):
        try:
            for sub in os.listdir(base):
                p = os.path.join(base, sub)
                if os.path.isdir(p) and ("僵尸" in sub or sub.lower().startswith("kka")):
                    if p not in roots:
                        roots.append(p)
        except OSError:
            pass
    return roots


def collect_sources():
    """返回 [(来源说明, 目录, [文件...])]"""
    found, seen = [], set()
    for base in candidate_roots():
        for name in LOG_DIR_NAMES:
            d = os.path.join(base, name)
            if not os.path.isdir(d) or d in seen:
                continue
            seen.add(d)
            files = []
            for pat in PATTERNS:
                files += glob.glob(os.path.join(d, pat))
            files = [f for f in files if os.path.isfile(f)]
            if files:
                found.append((os.path.relpath(d, base) or name, d, files))
    for env, label in (("APPDATA", "APPDATA_KKAuto"), ("TEMP", "TEMP_KKAuto")):
        d = os.path.join(os.environ.get(env, ""), "KKAuto")
        if not os.path.isdir(d) or d in seen:
            continue
        seen.add(d)
        files = [f for f in glob.glob(os.path.join(d, "*")) if os.path.isfile(f)]
        if files:
            found.append((label, d, files))
    return found


def pick_recent(files, limit=PER_SOURCE_LIMIT):
    try:
        files = sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)
    except OSError:
        pass
    return files[:limit]


def _read_write(src: str, dst: str):
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(data)


def _copy(src: str, dst: str):
    """复制一个文件：成功返回 None，失败返回最后一次的错误描述

    顺序：copy2（连时间戳/属性一起）→ 纯内容 copyfile → 读完再写 → 隔 200ms 再来一轮。
    2026-11-18 16:39 实测：GUI 里点按钮时有 1 个 bot_*.log 反复失败，
    事后手动 copy2 又是好的 —— 说明失败往往是「属性/时间戳没写进去」（copystat 被拒）
    或杀软短暂占用，不是内容读不出来。所以兜底要分两级，并且把原因记进报告里。
    """
    err = ""
    for attempt in (0, 1):
        for fn in (lambda: shutil.copy2(src, dst),
                   lambda: shutil.copyfile(src, dst),
                   lambda: _read_write(src, dst)):
            try:
                fn()
                return None
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        if attempt == 0:
            time.sleep(0.2)
    return err or "未知错误"


def build(out_dir: str, extra_text: str | None = None,
          extra_name: str = "界面日志_快照.txt") -> dict:
    """把日志收进 out_dir，返回 {'out','total','copied','failed','report'}"""
    os.makedirs(out_dir, exist_ok=True)
    lines = [
        "KK 日志收集包",
        "生成时间: " + time.strftime("%Y-%m-%d %H:%M:%S"),
        "工具位置: " + exe_dir(),
        "",
        "== 环境 ==",
        screen_info(),
        "",
        windows_report(),
        "",
    ]

    total, copied, failed = 0, 0, []
    for label, src, files in collect_sources():
        keep = pick_recent(files)
        safe = label.replace("\\", "_").replace(":", "").replace("%", "")
        dst = os.path.join(out_dir, safe)
        os.makedirs(dst, exist_ok=True)
        lines.append(f"== 日志来源 {label} ==")
        lines.append(f"   共 {len(files)} 个文件，取最新 {len(keep)} 个")
        for f in keep:
            total += 1
            err = _copy(f, os.path.join(dst, os.path.basename(f)))
            if err is None:
                copied += 1
            else:
                failed.append(os.path.basename(f))
                lines.append(f"   复制失败 {os.path.basename(f)}  ({err})")
        lines.append("")

    # ---- 失败现场截图（logs\miss\*.png 等），只取最新几张 ----
    # 2026-11-26 修：抓图实际落在「程序根目录\logs\shot」（打包后 dbgprobe 的
    # _core() 就是 exe 目录），而这里原来只在 collect_sources() 的结果里找 ——
    # 根目录 logs 里没有 .log/.json 时不进 collect_sources，于是图一张都没带进包。
    # 现在直接按候选根目录把 logs\shot、logs\miss 全扫一遍。
    shot_files = []
    shot_dirs, seen_dirs = [], set()
    for base in candidate_roots():
        for name in LOG_DIR_NAMES + ("",):
            for sub in SHOT_DIRS:
                d = os.path.join(base, name, sub) if name else os.path.join(base, sub)
                if os.path.isdir(d) and d not in seen_dirs:
                    seen_dirs.add(d)
                    shot_dirs.append(d)
    for d in shot_dirs:
        shot_files += glob.glob(os.path.join(d, "*.png"))
        shot_files += glob.glob(os.path.join(d, "*_windows.txt"))
    seen_shots, picked = set(), []
    for f in sorted(set(shot_files), key=lambda p: os.path.getmtime(p), reverse=True):
        if f in seen_shots:
            continue
        seen_shots.add(f)
        try:
            sz = os.path.getsize(f)
        except OSError:
            continue
        if len(picked) >= SHOT_LIMIT or sum(s for _, s in picked) + sz > SHOT_MAX_BYTES:
            break
        picked.append((f, sz))
    if picked:
        d = os.path.join(out_dir, "截图")
        os.makedirs(d, exist_ok=True)
        lines.append("== 截图（失败现场 miss / 手动抓图 shot，最新几张）==")
        for f, sz in picked:
            total += 1
            err = _copy(f, os.path.join(d, os.path.basename(f)))
            if err is None:
                copied += 1
                lines.append(f"   {os.path.basename(f)}  {sz // 1024} KB")
            else:
                failed.append(os.path.basename(f))
                lines.append(f"   复制失败 {os.path.basename(f)}  ({err})")
        lines.append("")

    # ---- 调试现场（2026-11-25 用户要求加调试开关）：整个 logs\debug 带上 ----
    # 这里不按「最新 12 张」挑：调试会话本身就是人为筛过的证据（点击前截图带十字、
    # 模板打分 json、环境 json），少一张就可能对不上号。数量由 dbgprobe 自己卡上限。
    dbg_files = []
    for base in candidate_roots():
        for name in LOG_DIR_NAMES:
            d = os.path.join(base, name, "debug")
            if not os.path.isdir(d):
                continue
            for cur, _dirs, fs in os.walk(d):
                for fn in fs:
                    dbg_files.append(os.path.join(cur, fn))
    if dbg_files:
        d = os.path.join(out_dir, "调试")
        os.makedirs(d, exist_ok=True)
        lines.append("== 调试现场（logs\\debug）==")
        keep, total_b, n = [], 0, 0
        for f in sorted(set(dbg_files), key=lambda p: os.path.getmtime(p), reverse=True):
            try:
                sz = os.path.getsize(f)
            except OSError:
                continue
            if n >= DBG_LIMIT or total_b + sz > DBG_MAX_BYTES:
                lines.append(f"   （超过 {DBG_LIMIT} 个 / {DBG_MAX_BYTES // 1024 // 1024}MB 上限，"
                             f"其余 {len(dbg_files) - n} 个没带上）")
                break
            rel = os.path.relpath(f, os.path.dirname(os.path.dirname(f)))
            err = _copy(f, os.path.join(d, rel.replace(os.sep, "_")))
            total += 1
            if err is None:
                copied += 1
                n += 1
                total_b += sz
                lines.append(f"   {rel}  {sz // 1024} KB")
            else:
                failed.append(os.path.basename(f))
        lines.append("")

    if extra_text:
        # 界面上的日志是「倒序显示」的，快照原样保存（开头是最新的），看时间戳排序即可
        with open(os.path.join(out_dir, extra_name), "w", encoding="utf-8") as f:
            f.write("（界面显示顺序：最新在最上面）\n")
            f.write("=" * 60 + "\n")
            f.write(extra_text)

    lines.append(f"合计: 发现 {total} 个文件，复制成功 {copied} 个")
    if total == 0:
        lines += [
            "",
            "⚠ 没找到任何日志。可能原因：",
            "   1) 工具没放在成品目录里（应和 我在峡谷打僵尸_vX.X.exe 同级）",
            "   2) 还没运行过成品（logs 目录是运行时才建的）",
        ]
    report = "\n".join(lines)
    with open(os.path.join(out_dir, "环境与说明.txt"), "w", encoding="utf-8") as f:
        f.write(report)
    return {"out": out_dir, "total": total, "copied": copied,
            "failed": failed, "report": report}


def zip_dir(folder: str, zip_path: str | None = None) -> str:
    """把整个文件夹压成一个 zip（内含同名的顶层目录），返回 zip 路径"""
    if not zip_path:
        zip_path = folder.rstrip("\\/") + ".zip"
    root = os.path.dirname(folder.rstrip("\\/"))
    base = os.path.basename(folder.rstrip("\\/"))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for cur, _dirs, files in os.walk(folder):
            for name in files:
                p = os.path.join(cur, name)
                z.write(p, os.path.join(base, os.path.relpath(p, folder)))
    return zip_path


def default_out_dir() -> str:
    return os.path.join(desktop_dir(), APP + "_" + time.strftime("%Y%m%d_%H%M%S"))


def pack(out_dir: str | None = None, extra_text: str | None = None,
         make_zip: bool = True) -> dict:
    """一键：收集 → 压 zip。返回 build() 的 dict + 'zip' 键"""
    out_dir = out_dir or default_out_dir()
    info = build(out_dir, extra_text=extra_text)
    info["zip"] = ""
    if make_zip:
        try:
            info["zip"] = zip_dir(out_dir)
        except Exception as e:            # 压缩失败不影响文件夹本身可用
            info["zip_error"] = repr(e)
    return info
