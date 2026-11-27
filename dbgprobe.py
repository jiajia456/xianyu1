# -*- coding: utf-8 -*-
"""调试探针 —— 把「坐标不对 / 图色认不出」的现场整包留下来

背景（2026-11-25 用户）：拿成品去 Win7 那台机器测，怕出现坐标或图色异常导致
点了没反应。没有现场就只能靠猜，所以这里做三件事：

1. **全程记录**（界面勾「调试」= 环境变量 KK_DEBUG=1）
   每次模板匹配记一条：模板名、阈值、实际最高分、命中位置、窗口客户区、缩放系数；
   每次点击前存一张带十字标记的截图（限流 + 限量，见 MAX_SHOTS）。
   子进程（full_loop / game_auto / in_game …）靠环境变量继承，父进程建好目录后
   用 KK_DEBUG_DIR 传下去，保证一份会话一个目录，不会各写各的。

2. **抓现场**（界面「抓现场」按钮）
   立刻存：全屏截图 + 每个「像游戏窗」的客户区截图 + 环境信息（屏幕/DPI/缩放/显示器）
   + 全部模板在当前画面上的**实际最高分**（按分数排序）。
   这一份就能分清是「窗口尺寸/缩放变了（坐标问题）」还是「画面渲染/取色变了（图色问题）」。

3. **随日志一起发回来**
   logpack 会把 logs/debug/ 整个打包，用户点「打包日志发我」即可。

本模块不允许影响正常运行：所有函数先看 on()，关着的时候只有一次字符串比较。
"""
from __future__ import annotations

import json
import os
import threading
import time

# ---- 硬上限（调试模式可能连跑几小时，必须有闸）----
MAX_SHOTS = 40                      # 截图张数上限
MAX_SHOT_BYTES = 28 * 1024 * 1024   # 截图总量上限
MAX_LINES = 20000                   # 事件行上限
SHOT_MIN_GAP = 1.2                  # 两张截图至少隔这么久（秒）
MISS_NOTE_GAP = 1.0                 # 同一个模板「没命中」最多每秒记一条

_lock = threading.Lock()
_S = {
    "dir": "",
    "shots": 0,
    "bytes": 0,
    "lines": 0,
    "last_shot": 0.0,
    "shot_paths": [],   # 先进先出：满了就挤掉最旧的，保证「最近 40 张」才是有用的
    "hit": None,        # 最近一次命中的 {name, score, x, y, w, h, ts}
    "miss": {},         # 模板名 → 上次记 miss 的时间
    "warned": False,
}


def on() -> bool:
    """调试模式是否开启（读环境变量，子进程会继承）"""
    return (os.environ.get("KK_DEBUG") or "").strip() == "1"


def _core() -> str:
    """core 目录：源码里就是本文件所在目录；打包后 core 仍与 exe 同级"""
    return os.path.dirname(os.path.abspath(__file__))


def session_dir() -> str:
    """本次调试会话目录。优先用 KK_DEBUG_DIR（父进程指定，子进程复用）"""
    d = (os.environ.get("KK_DEBUG_DIR") or "").strip()
    if not d:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        d = os.path.join(_core(), "logs", "debug", f"{stamp}_p{os.getpid()}")
        os.environ["KK_DEBUG_DIR"] = d       # 让后面 spawn 的子进程进同一个目录
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.join(os.path.dirname(_core()), "logs", "debug",
                         time.strftime("%Y%m%d_%H%M%S"))
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return ""
    if not _S["dir"]:
        _S["dir"] = d
    return d


def _write_json(path: str, data) -> str:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        return path
    except Exception:
        return ""


def note(tag: str, **fields) -> None:
    """往 events.jsonl 追加一条。tag 用英文短词，方便 grep"""
    if not on():
        return
    d = session_dir()
    if not d:
        return
    with _lock:
        if _S["lines"] >= MAX_LINES:
            return
        _S["lines"] += 1
        rec = {"ts": time.strftime("%H:%M:%S"), "pid": os.getpid(), "tag": tag}
        rec.update(fields)
        try:
            with open(os.path.join(d, "events.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------- 截图
def _save_png(path: str, img) -> bool:
    """中文路径必须 imencode + tofile（cv2.imwrite 会静默失败）"""
    try:
        import cv2
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return False
        with open(path, "wb") as f:
            buf.tofile(f)
        return True
    except Exception:
        return False


def _annotate(img, marks, title: str = ""):
    """在图上画十字 + 文字标注（marks: [(x, y, label), ...]）"""
    try:
        import cv2
        out = img.copy()
        h, w = out.shape[:2]
        for i, m in enumerate(marks or []):
            x, y = int(m[0]), int(m[1])
            label = str(m[2]) if len(m) > 2 else ""
            color = (0, 0, 255) if i == 0 else (0, 200, 255)
            cv2.line(out, (x - 14, y), (x + 14, y), color, 2)
            cv2.line(out, (x, y - 14), (x, y + 14), color, 2)
            cv2.circle(out, (x, y), 7, color, 2)
            if label:
                ty = max(14, y - 18)
                cv2.putText(out, label, (max(2, x - 60), ty),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
        if title:
            cv2.rectangle(out, (0, 0), (w, 22), (32, 32, 32), -1)
            cv2.putText(out, title, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (255, 255, 255), 1, cv2.LINE_AA)
        return out
    except Exception:
        return img


def shot(img, tag: str, marks=None, title: str = "", force: bool = False) -> str:
    """存一张（可带十字标记）的截图，返回路径；关着/到上限返回 "" """
    if not on() or img is None:
        return ""
    d = session_dir()
    if not d:
        return ""
    now = time.time()
    with _lock:
        if not force:
            if now - _S["last_shot"] < SHOT_MIN_GAP:
                return ""
            # 额度满了：删最旧的一张再存 —— 长跑时真正出问题的是后面那几步，
            # 「存满 40 张就停」会把最有用的现场挡在门外（2026-11-25 改）。
            while len(_S["shot_paths"]) >= MAX_SHOTS:
                old = _S["shot_paths"].pop(0)
                try:
                    if os.path.exists(old):
                        _S["bytes"] -= os.path.getsize(old)
                        os.remove(old)
                except Exception:
                    pass
        _S["last_shot"] = now
        _S["shots"] += 1
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (tag or "shot"))[:40]
    path = os.path.join(d, f"{time.strftime('%H%M%S')}_{safe}.png")
    img2 = _annotate(img, marks, title or tag)
    if not _save_png(path, img2):
        return ""
    try:
        with _lock:
            _S["shot_paths"].append(path)
            _S["bytes"] += os.path.getsize(path)
            # 字节预算同样按「挤掉最旧的」处理，别把刚存的新图删了
            while _S["bytes"] > MAX_SHOT_BYTES and len(_S["shot_paths"]) > 1:
                old = _S["shot_paths"].pop(0)
                try:
                    if os.path.exists(old):
                        _S["bytes"] -= os.path.getsize(old)
                        os.remove(old)
                except Exception:
                    pass
    except Exception:
        pass
    note("shot", file=os.path.basename(path), label=tag, n_marks=len(marks or []))
    return path


# ---------------------------------------------------------------- 找图 / 点击钩子
def hit_context() -> dict:
    """最近一次命中的上下文（点击时用来标注「点的是哪个模板、分数多少」）"""
    h = _S.get("hit")
    if not h:
        return {}
    if time.time() - float(h.get("ts", 0)) > 3.0:
        return {}
    return h


def on_find(name: str, threshold: float, hit, screen, bot=None,
            region=None, scales=None) -> None:
    """由 game_auto.Bot.find 调用：命中必记，没命中有节流地记（分数是图色异常的关键证据）"""
    if not on():
        return
    ctx = {}
    try:
        if bot is not None:
            ox, oy, cw, ch = bot.win.client_rect()
            kx, ky = bot._kxy()
            ctx = {"win": [cw, ch], "origin": [ox, oy], "k": [round(kx, 3), round(ky, 3)]}
    except Exception:
        pass
    shape = None
    try:
        shape = [int(screen.shape[1]), int(screen.shape[0])]
    except Exception:
        pass
    if hit:
        ok, x, y, w, h, score = hit
        _S["hit"] = {"name": name, "score": round(float(score), 4), "x": x, "y": y,
                     "w": w, "h": h, "ts": time.time(), "img": screen}
        note("find_hit", tpl=name, score=round(float(score), 4),
             th=threshold, at=[x, y], size=[w, h], frame=shape,
             region=list(region) if region else None, **ctx)
        return
    now = time.time()
    last = _S["miss"].get(name, 0.0)
    if now - last < MISS_NOTE_GAP:
        return
    _S["miss"][name] = now
    note("find_miss", tpl=name, th=threshold, frame=shape,
         region=list(region) if region else None, **ctx)


def on_click(bot, x: int, y: int, clicks: int = 1, hover: float = 0.0,
             button: str = "left") -> None:
    """由 game_auto.Bot.click / click_hover 调用：存「点击前」的画面 + 记录坐标换算"""
    if not on():
        return
    ctx, img = {}, None
    try:
        ox, oy, cw, ch = bot.win.client_rect()
        kx, ky = bot._kxy()
        ctx = {"client": [cw, ch], "origin": [ox, oy],
               "k": [round(kx, 3), round(ky, 3)],
               "screen_xy": [ox + int(round(x * kx)), oy + int(round(y * ky))]}
    except Exception:
        pass
    h = hit_context()
    label = f"{h['name']}({h['score']:.3f})→({x},{y})" if h else f"click→({x},{y})"
    try:
        img = h.get("img") if h else None
        if img is None:
            img = bot.grab(bot.win)
    except Exception:
        img = None
    title = f"{'hover' if hover else 'click'} x{clicks} {label}"
    if img is not None:
        shot(img, "click_" + (h.get("name", "pos") if h else "pos"),
             marks=[(x, y, label)], title=title)
    note("click", at=[x, y], clicks=clicks, button=button,
         tpl=(h or {}).get("name"), score=(h or {}).get("score"),
         hover=round(float(hover), 2) if hover else 0, **ctx)
    _S["hit"] = None


# ---------------------------------------------------------------- 环境 / 窗口
def env_info() -> dict:
    """屏幕、DPI、缩放、显示器、系统版本 —— 坐标问题的第一现场"""
    import sys
    info = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.version.split()[0],
            "exe": sys.executable,
            "frozen": bool(getattr(sys, "frozen", False)),
            "cwd": os.getcwd(), "core": _core()}
    try:
        import ctypes
        u = ctypes.windll.user32
        info["screen"] = [u.GetSystemMetrics(0), u.GetSystemMetrics(1)]
        try:
            info["virtual_screen"] = [u.GetSystemMetrics(78), u.GetSystemMetrics(79)]
        except Exception:
            pass
        try:
            hdc = u.GetDC(0)
            g = ctypes.windll.gdi32
            dpi = g.GetDeviceCaps(hdc, 88)      # LOGPIXELSX
            u.ReleaseDC(0, hdc)
            info["system_dpi"] = dpi
            info["system_scale"] = round(dpi / 96.0, 3)
        except Exception:
            pass
        try:
            val = ctypes.c_int()
            # 0=unaware 1=system 2=per-monitor（Win8.1+ 才有 shcore）
            ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(val))
            info["dpi_awareness"] = val.value
        except Exception:
            info["dpi_awareness"] = "n/a (无 shcore，Win7 走 SetProcessDPIAware)"
        try:
            info["admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            pass
    except Exception as e:
        info["env_error"] = repr(e)
    try:
        import platform
        info["platform"] = platform.platform()
    except Exception:
        pass
    return info


def windows_info() -> list:
    """枚举窗口：标题、类名、pid、exe、矩形（用于判断绑的是不是对的窗口）"""
    out = []
    try:
        import game_auto as ga
        for w in ga.enum_windows_full():
            rect = None
            try:
                import win32gui
                rect = tuple(int(v) for v in win32gui.GetWindowRect(w.get("hwnd")))
            except Exception:
                rect = None
            out.append({"title": w.get("title"), "cls": w.get("cls"),
                        "pid": w.get("pid"), "exe": w.get("exe"),
                        "w": w.get("w"), "h": w.get("h"),
                        "rect": rect})
    except Exception:
        pass
    return out


def _grab_rect(x, y, w, h):
    try:
        import mss
        import numpy as np
        import cv2
        with mss.mss() as sct:
            raw = sct.grab({"left": int(x), "top": int(y),
                            "width": int(w), "height": int(h)})
            return cv2.cvtColor(np.array(raw, dtype=np.uint8), cv2.COLOR_BGRA2BGR)
    except Exception:
        return None


def _grab_screen_gdi():
    """整屏抓图：GDI BitBlt + CAPTUREBLT（会带上分层窗口）

    为什么不用 ImageGrab：它内部是纯 SRCCOPY 的 BitBlt，**漏掉 WS_EX_LAYERED**。
    2026-11-26 现场实证：平台的 S41 活动广告弹窗（半透明、淡入）屏幕上明明看得见，
    ImageGrab 抓出来却只剩底下的大厅页面，弹窗整块消失 —— 抓不到证据。
    CAPTUREBLT 就是给这种窗口用的（MSDN：Includes any windows that are layered
    on top of your window in the resulting image）。
    """
    try:
        import ctypes
        from ctypes import wintypes
        import numpy as np
    except Exception:
        return None

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    hdc = mem = bmp = None
    try:
        vx = user32.GetSystemMetrics(76)      # SM_XVIRTUALSCREEN
        vy = user32.GetSystemMetrics(77)      # SM_YVIRTUALSCREEN
        vw = user32.GetSystemMetrics(78)      # SM_CXVIRTUALSCREEN
        vh = user32.GetSystemMetrics(79)      # SM_CYVIRTUALSCREEN
        if vw <= 0 or vh <= 0:
            return None
        hdc = user32.GetDC(None)
        if not hdc:
            return None
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, vw, vh)
        if not mem or not bmp:
            return None
        gdi32.SelectObject(mem, bmp)
        SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
        if not gdi32.BitBlt(mem, 0, 0, vw, vh, hdc, vx, vy, SRCCOPY | CAPTUREBLT):
            return None

        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth = vw
        bi.biHeight = -vh                     # 负数 = 行序自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0                  # BI_RGB
        buf = ctypes.create_string_buffer(vw * vh * 4)
        if not gdi32.GetDIBits(mem, bmp, 0, vh, buf, ctypes.byref(bi), 0):
            return None
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(vh, vw, 4)
        return arr[:, :, :3].copy()           # BGRA -> BGR
    except Exception:
        return None
    finally:
        try:
            if bmp:
                gdi32.DeleteObject(bmp)
            if mem:
                gdi32.DeleteDC(mem)
            if hdc:
                user32.ReleaseDC(None, hdc)
        except Exception:
            pass


def _grab_screen():
    """整屏抓图：先走 CAPTUREBLT（带分层窗口），失败再退回 ImageGrab"""
    img = _grab_screen_gdi()
    if img is not None:
        return img
    try:
        from PIL import ImageGrab
        import numpy as np
        import cv2
        img = ImageGrab.grab()
        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def grab_desktop(tag: str = "shot", out_dir: str = "") -> str:
    """抓一张整屏 PNG，返回文件路径（失败返回空串）。

    「抓图」按钮专用：调用方先把本软件自己的窗口藏起来，再调这里，最后放回来，
    这样截出来的图里不会带自己的界面。默认存 core\\logs\\shot\\。
    """
    img = _grab_screen()
    if img is None:
        return ""
    d = out_dir or os.path.join(_core(), "logs", "shot")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return ""
    p = os.path.join(d, f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}.png")
    if not _save_png(p, img):
        return ""
    # 附一份窗口清单：万一哪台机器上弹窗还是抓不进像素，至少能知道当时屏幕上有谁、
    # 类名/PID/矩形是什么，顺着这个就能单独去 PrintWindow 它。
    try:
        lines = [f"{time.strftime('%Y-%m-%d %H:%M:%S')} 抓图时的窗口清单（按面积从大到小）："]
        for w in windows_info()[:20]:
            r = w.get("rect") or (0, 0, 0, 0)
            ww = (r[2] - r[0]) if r[2] else (w.get("w") or 0)
            hh = (r[3] - r[1]) if r[3] else (w.get("h") or 0)
            if ww < 120 or hh < 80:
                continue
            lines.append(f"  {w.get('title')!r}  cls={w.get('cls')}  "
                         f"exe={w.get('exe')}  pid={w.get('pid')}  "
                         f"rect={tuple(r)}  {ww}x{hh}")
        with open(p[:-4] + "_windows.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass
    return p


# ---------------------------------------------------------------- 模板打分
REF_SIZES = ((1690, 921), (1332, 945), (1224, 904), (1920, 1080), (1264, 921))


def score_templates(img, tpl_dir: str, top_n: int = 12) -> list:
    """把 tpl_dir 里所有模板在当前画面上打一遍分（半尺寸粗算 + 前几名全尺寸复核）

    这是判断「图色异常」最直接的证据：同一张模板，在正常机器上 0.98，
    在这台机器上只有 0.6x，就是渲染/取色/缩放的问题，不是点击逻辑的问题。
    """
    rows = []
    try:
        import cv2
        import numpy as np
    except Exception:
        return rows
    if img is None:
        return rows
    h, w = img.shape[:2]
    raw = [1.0]
    for rw, rh in REF_SIZES:
        fx, fy = w / float(rw), h / float(rh)
        if 0.4 <= fx <= 1.6 and 0.4 <= fy <= 1.6:
            raw.append((fx, fy))
    # 尺度去重 + 最多 3 档：1690/1332/1224 这几个参考尺寸算出来的比例很接近，
    # 逐个都跑等于白烧 CPU（实测 90 张模板 × 5 档要 17 秒，压到 3 档约 8 秒）。
    scales = []
    for s in raw:
        fx, fy = (s, s) if isinstance(s, (int, float)) else (float(s[0]), float(s[1]))
        if any(abs(fx - px) <= 0.05 * max(1.0, px) and abs(fy - py) <= 0.05 * max(1.0, py)
               for px, py in scales):
            continue
        scales.append((fx, fy))
        if len(scales) >= 3:
            break
    half = cv2.resize(img, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    try:
        names = sorted(n for n in os.listdir(tpl_dir) if n.lower().endswith(".png"))
    except Exception:
        return rows
    for name in names:
        tpl = None
        try:
            tpl = cv2.imdecode(np.fromfile(os.path.join(tpl_dir, name), np.uint8),
                               cv2.IMREAD_COLOR)
        except Exception:
            tpl = None
        if tpl is None or min(tpl.shape[:2]) < 6:
            continue
        best = (-1.0, None, None)
        for s in scales:
            fx, fy = (s, s) if isinstance(s, (int, float)) else s
            tw = max(6, int(round(tpl.shape[1] * fx * 0.5)))
            th = max(6, int(round(tpl.shape[0] * fy * 0.5)))
            if half.shape[0] < th or half.shape[1] < tw:
                continue
            try:
                ts = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
                res = cv2.matchTemplate(half, ts, cv2.TM_CCOEFF_NORMED)
                _, mv, _, ml = cv2.minMaxLoc(res)
            except Exception:
                continue
            if mv > best[0]:
                best = (float(mv), s, ml)
        if best[0] < 0:
            continue
        rows.append({"tpl": name, "score_half": round(best[0], 4),
                     "scale": best[1], "loc_half": list(best[2]) if best[2] else None})
    rows.sort(key=lambda r: -r["score_half"])
    # 前几名用全尺寸复核（半尺寸分数会略低，差 0.01~0.03）
    for r in rows[:top_n]:
        try:
            tpl = cv2.imdecode(np.fromfile(os.path.join(tpl_dir, r["tpl"]), np.uint8),
                               cv2.IMREAD_COLOR)
            s = r["scale"]
            fx, fy = (s, s) if isinstance(s, (int, float)) else s
            tw, th = max(6, int(round(tpl.shape[1] * fx))), max(6, int(round(tpl.shape[0] * fy)))
            if img.shape[0] >= th and img.shape[1] >= tw:
                ts = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
                res = cv2.matchTemplate(img, ts, cv2.TM_CCOEFF_NORMED)
                _, mv, _, ml = cv2.minMaxLoc(res)
                r["score"] = round(float(mv), 4)
                r["loc"] = [int(ml[0]), int(ml[1])]
        except Exception:
            pass
    return rows


# ---------------------------------------------------------------- 一键抓现场
def snapshot(bot=None, win=None, tag: str = "现场", score_tpl: bool = True) -> dict:
    """把当前现场整套存下来，返回 {dir, files, summary}

    存什么：环境.json、窗口.json、屏幕.png、窗口_*.png、模板打分.json
    界面「抓现场」按钮调这个，用户点完直接「打包日志发我」发回来。
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    d = session_dir() or os.path.join(_core(), "logs", "debug", stamp)
    os.makedirs(d, exist_ok=True)
    sub = os.path.join(d, f"snap_{stamp}_{tag}")
    os.makedirs(sub, exist_ok=True)
    made, summary = [], []

    env = env_info()
    p = _write_json(os.path.join(sub, "环境.json"), env)
    if p:
        made.append(p)
        summary.append(f"屏幕 {env.get('screen')} 缩放 {env.get('system_scale')} "
                       f"DPI感知 {env.get('dpi_awareness')}")

    wins = windows_info()
    if bot is not None:
        try:
            ox, oy, cw, ch = bot.win.client_rect()
            kx, ky = bot._kxy()
            env["bound_window"] = {"client": [cw, ch], "origin": [ox, oy],
                                   "k": [round(kx, 3), round(ky, 3)]}
            summary.append(f"已绑定窗口 {cw}x{ch} @({ox},{oy}) 缩放 k={kx:.3f}/{ky:.3f}")
        except Exception:
            pass
    p = _write_json(os.path.join(sub, "窗口.json"), wins)
    if p:
        made.append(p)
    summary.append(f"枚举到 {len(wins)} 个窗口")

    screen = _grab_screen()
    if screen is not None:
        f = os.path.join(sub, "屏幕.png")
        if _save_png(f, screen):
            made.append(f)

    # 逐个窗口客户区截图（只取像样的：面积够大）
    n_win = 0
    for i, w in enumerate(wins[:6]):
        try:
            x, y, cw, ch = w.get("rect") or (0, 0, 0, 0)
            if cw < 400 or ch < 300:
                continue
            img = _grab_rect(x, y, cw, ch)
            if img is None:
                continue
            f = os.path.join(sub, f"窗口{i + 1}_{cw}x{ch}.png")
            if _save_png(f, img):
                made.append(f)
                n_win += 1
        except Exception:
            continue
    if n_win:
        summary.append(f"窗口截图 {n_win} 张")

    # 绑定窗口 / 游戏窗画面（识别用），并给它打分
    target, target_name = None, ""
    if bot is not None:
        try:
            target = bot.grab(bot.win)
            target_name = "bound"
        except Exception:
            target = None
    if target is None and wins:
        # 优先拿「面积最大的窗口」= 通常是游戏窗（1690x921）而不是平台主窗（1332x945）。
        # 排在前面的不一定是游戏窗，按枚举顺序取第一个会经常抓到平台窗。
        cands = []
        for w in wins:
            rect = w.get("rect") or (0, 0, 0, 0)
            if rect[2] >= 600 and rect[3] >= 450:
                cands.append((rect[2] * rect[3], rect))
        for _area, rect in sorted(cands, reverse=True)[:1]:
            target = _grab_rect(*rect)
            target_name = f"win{rect[2]}x{rect[3]}"
    if target is None:
        target, target_name = screen, "screen"
    if target is not None:
        f = os.path.join(sub, f"识别用画面_{target_name}.png")
        if _save_png(f, target):
            made.append(f)
        env["bound_window"] = env.get("bound_window") or {}
        _write_json(os.path.join(sub, "环境.json"), env)

    rows = []
    if score_tpl and target is not None:
        rows = score_templates(target, os.path.join(_core(), "templates"))
        _write_json(os.path.join(sub, "模板打分.json"), rows)
        good = [r for r in rows if r["score_half"] >= 0.85]
        top = rows[0] if rows else None
        summary.append(f"模板 {len(rows)} 张，≥0.85 的 {len(good)} 张"
                       + (f"，最高 {top['tpl']} {top['score_half']:.3f}" if top else ""))

    note("snapshot", dir=sub, files=len(made), tpl_rows=len(rows), label=tag)
    return {"dir": sub, "files": made, "summary": summary, "templates": rows}
