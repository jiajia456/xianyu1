# -*- coding: utf-8 -*-
"""本机系统识别 + 按系统分档的运行参数（2026-11-25 用户要求）

用户原话：「点击开始运行，就检测本机系统环境是 W7 还是 W10，因为会不一样系统
导致有些操作分歧」。

真实存在的分歧（不是想象出来的）：
  1. **DPI 感知**：Win8.1+ 才有 shcore.SetProcessDpiAwareness（per-monitor），
     Win7 只能 user32.SetProcessDPIAware（system-aware）。截图与点击坐标的
     「物理像素 / 逻辑像素」关系就在这一步定下来。
  2. **后台抓图**：Win7 关掉 Aero（无 DWM 合成）时，PrintWindow 经常返回黑图，
     只能退回屏幕抓取；Win10 一直合成，PrintWindow 一般可用。
  3. **老机器节奏**：Win7 那批机器渲染和响应普遍慢半拍，鼠标 hover 时间要放宽，
     否则「刚移上去就按下」War3 不认（这条 11-16 就踩过）。

所以这里只做两件事：
  · 判定本机是哪一档（win7 / win8 / win81 / win10 / win11 / other），写日志、落盘
  · 按档位取参数（configs/os_profile.json 可改，不写死在代码里）

注意：判定结果**不改游戏逻辑**，只影响「抓图方式 / 悬停时长」这类边界参数。
"""
from __future__ import annotations

import json
import os
import sys

_lock = {"info": None}
_PROFILES = ("win7", "win8", "win81", "win10", "win11", "other")

# 没有配置文件时的内置默认（配置文件只覆盖它有的键）
BUILTIN = {
    "default": {"hover_scale": 1.0, "capture_mode": "auto", "focus_wait_scale": 1.0},
    "win7": {"hover_scale": 1.0, "capture_mode": "auto", "focus_wait_scale": 1.0,
             "note": "v1.94：未实测，先与 Win10 一致；测出分歧再按 os_profile.json 调"},
    "win10": {"hover_scale": 1.0, "capture_mode": "auto", "focus_wait_scale": 1.0},
    "win11": {"hover_scale": 1.0, "capture_mode": "auto", "focus_wait_scale": 1.0},
}


def _core() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _pick(d: dict, keys) -> dict:
    """只挑认识的键（不填充默认值）——填充会把档位专属的值又冲回默认"""
    return {k: d[k] for k in keys if isinstance(d, dict) and k in d}


_KEYS = ("hover_scale", "focus_wait_scale", "capture_mode", "save_panel_score", "note")


def classify(major: int, minor: int, build: int) -> tuple:
    """版本号 → (profile, 显示名)。纯函数，方便离线测试（不依赖真机）"""
    if (major, minor) == (6, 1):
        return "win7", "Windows 7"
    if (major, minor) == (6, 2):
        return "win8", "Windows 8"
    if (major, minor) == (6, 3):
        return "win81", "Windows 8.1"
    if (major, minor) == (10, 0):
        # Win11 的版本号还是 10.0，只能靠 build 区分（22000 起是 Win11）
        return ("win11", "Windows 11") if build >= 22000 else ("win10", "Windows 10")
    if (major, minor) == (6, 0):
        return "other", "Windows Vista"
    return "other", f"Windows {major}.{minor}"


def info(refresh: bool = False) -> dict:
    """本机系统信息（缓存；测试里可 info(True) 强制重算）"""
    if _lock["info"] is not None and not refresh:
        return _lock["info"]
    out = {"platform": "unknown", "major": 0, "minor": 0, "build": 0,
           "sp": "", "arch": "", "profile": "other", "name": "未知系统"}
    try:
        v = sys.getwindowsversion()
        out["major"], out["minor"], out["build"] = int(v.major), int(v.minor), int(v.build)
        sp = getattr(v, "service_pack", "") or ""
        out["sp"] = str(sp).strip()
    except Exception:
        pass
    try:
        import platform as _p
        out["arch"] = _p.machine()
        out["platform"] = _p.platform()
    except Exception:
        pass
    out["profile"], out["name"] = classify(out["major"], out["minor"], out["build"])
    if out["sp"] and out["profile"] in ("win7", "win8", "win81", "other"):
        out["name"] += " " + out["sp"]
    out["admin"] = _is_admin()
    out.update(_display())
    _lock["info"] = out
    return out


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _display() -> dict:
    """屏幕分辨率 + 系统缩放（Win7 没有 GetDpiForSystem，退回 GetDeviceCaps）"""
    d = {}
    try:
        import ctypes
        u = ctypes.windll.user32
        d["screen"] = f"{u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)}"
        try:
            d["scale"] = round(int(u.GetDpiForSystem()) / 96.0, 2)     # Win10 1607+
        except Exception:
            hdc = u.GetDC(0)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))       # LOGPIXELSX
            u.ReleaseDC(0, hdc)
            d["scale"] = round(dpi / 96.0, 2)
    except Exception as e:
        d["display_err"] = repr(e)
    return d


def profile() -> str:
    return info()["profile"]


def is_win7() -> bool:
    return profile() == "win7"


def summary() -> str:
    """一行给人看的（写进界面日志 / sysenv.json 都用它）"""
    i = info()
    bits = [f"{i['name']}（{i['major']}.{i['minor']}.{i['build']}"]
    if i.get("arch"):
        bits[-1] += f"，{i['arch']}"
    bits[-1] += "）"
    if i.get("screen"):
        bits.append(f"屏幕 {i['screen']}")
    if i.get("scale"):
        bits.append(f"缩放 {int(round(i['scale'] * 100))}%")
    bits.append("管理员" if i.get("admin") else "非管理员")
    return "｜".join(bits)


def config_path() -> str:
    # KK_OS_PROFILE_CFG 只给测试/现场临时试参数用，正常走 core/configs/os_profile.json
    return os.environ.get("KK_OS_PROFILE_CFG") or os.path.join(_core(), "configs",
                                                              "os_profile.json")


def settings(prof: str = "") -> dict:
    """取当前档位的参数。

    优先级：配置文件(本档) > 配置文件(default) > 内置(本档) > 内置(默认)。
    注意别用「用默认值填满的 dict」去 update —— 那会把本档的值又冲回默认
    （2026-11-25 第一版就这么错的：Win7 的 1.25 被 1.0 覆盖，测试 ②b 抓到）。
    """
    prof = prof or profile()
    cfg = {}
    try:
        with open(config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    merged = dict(BUILTIN["default"])
    merged.update(_pick(BUILTIN.get(prof) or {}, _KEYS))
    merged.update(_pick(cfg.get("default") or {}, _KEYS))
    merged.update(_pick(cfg.get(prof) or {}, _KEYS))
    merged["profile"] = prof
    if isinstance(cfg, dict) and cfg.get("_force_capture_mode"):
        # 现场救急：不管什么系统，强制抓图方式（screen / printwindow / auto）
        merged["capture_mode"] = cfg["_force_capture_mode"]
    return merged


def hover_scale() -> float:
    try:
        return float(settings()["hover_scale"])
    except Exception:
        return 1.0


def focus_wait_scale() -> float:
    try:
        return float(settings()["focus_wait_scale"])
    except Exception:
        return 1.0


def capture_mode(default: str = "auto") -> str:
    """configs 里写 auto 时，按系统档位决定（目前两档都是 auto，留口子给现场调）"""
    try:
        m = str(settings()["capture_mode"])
    except Exception:
        m = default
    return m if m in ("auto", "screen", "printwindow") else default


def dump() -> str:
    """写 logs/sysenv.json，返回路径（失败返回 ""）"""
    i = info()
    i = dict(i)
    i["settings"] = settings()
    i["summary"] = summary()
    try:
        d = os.path.join(_core(), "logs")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "sysenv.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(i, f, ensure_ascii=False, indent=2)
        return p
    except Exception:
        return ""


def describe_effect() -> str:
    """把「这一档到底改了什么」说清楚，写进日志（用户看的就是这句）"""
    s = settings()
    parts = []
    if abs(float(s.get("hover_scale", 1.0)) - 1.0) > 1e-6:
        parts.append(f"悬停时长 ×{s['hover_scale']}")
    if abs(float(s.get("focus_wait_scale", 1.0)) - 1.0) > 1e-6:
        parts.append(f"等窗口前台 ×{s['focus_wait_scale']}")
    parts.append(f"抓图方式 {s.get('capture_mode', 'auto')}")
    # 只有放宽过才说明（W10 仍是 0.82，不打印），2026-11-25 W7 存档面板实测 0.769
    if float(s.get("save_panel_score", 0.82)) < 0.82:
        parts.append(f"存档面板阈值 {s['save_panel_score']}")
    return "、".join(parts)


if __name__ == "__main__":
    # 单机取证用：python sysenv.py（或 成品.exe core/sysenv.py）
    i = info()
    i["summary"] = summary()
    i["settings"] = settings()
    i["effect"] = describe_effect()
    print(json.dumps(i, ensure_ascii=False, indent=2))
