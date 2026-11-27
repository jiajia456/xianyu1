# -*- coding: utf-8 -*-
"""成品自检：在打包后的 core 里跑，逐个 import 子脚本真正用到的第三方库。

由 exe 的 dispatch 以 `_envcheck.py` 启动（cwd=core），结果写到 core/logs/envcheck.json，
因为 windowed exe 没有控制台，看不到 print。

2026-11-18 15:42：分成「必需 / 可选」两档。
  - 必需（REQUIRED）：core 脚本 import 了就会用，缺了功能直接坏 → 进 bad
  - 可选（OPTIONAL）：代码里有 try/except 兜底（如 psutil 只是 pid→exe 的加速路径，
    没装时 room_finder/cheat_guard 会退到 ctypes），缺了只记录、不算失败
"""
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "logs", "envcheck.json")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

# 缺了就是坏包
REQUIRED = [
    "cv2", "numpy", "PIL", "PIL.Image", "PyQt5.QtCore", "PyQt5.QtWidgets",
    "keyboard", "mss", "pydirectinput",
    "win32api", "win32con", "win32gui", "win32process", "win32ui", "win32clipboard",
    "win32timezone",
]
# 有兜底，缺了只提示
OPTIONAL = ["psutil", "pythoncom", "pywintypes"]
# 本地模块（不是 pip 包，就在 core/ 里）：界面的「打包日志发我」「调试 / 抓现场」直接 import 它们，
# 少了就是「按钮点了没反应」。2026-11-25 加：漏了 dbgprobe 时打包闸门看不出来。
sys.path.insert(0, HERE)
LOCAL = ["dbgprobe", "logpack", "sysenv"]

res = {"frozen": bool(getattr(sys, "frozen", False)),
       "executable": sys.executable,
       "meipass": getattr(sys, "_MEIPASS", None),
       "cwd": os.getcwd(),
       "here": HERE,
       "python": sys.version,
       "mods": {}}


def probe(name):
    """import 一下，顺便逼出延迟加载的二进制扩展"""
    m = __import__(name, fromlist=["*"])
    extra = ""
    try:
        if name == "keyboard":
            extra = "winkeyboard=%s" % hasattr(m, "_winkeyboard")
        elif name == "psutil":
            extra = "cpu=%s" % m.cpu_count()
        elif name == "mss":
            extra = str(m.__version__)
        elif name == "win32ui":
            extra = str(m.GetDoubleClickTime() if hasattr(m, "GetDoubleClickTime") else "ok")
    except Exception as e:
        extra = "attr-fail:%r" % (e,)
    return extra


for name in REQUIRED + OPTIONAL + LOCAL:
    need = "required" if (name in REQUIRED or name in LOCAL) else "optional"
    try:
        res["mods"][name] = {"ok": True, "extra": probe(name), "need": need}
    except Exception:
        res["mods"][name] = {"ok": False, "need": need,
                             "err": traceback.format_exc(limit=3)}

# 真按脚本的用法再试一遍
try:
    import keyboard
    keyboard.is_pressed("f10")
    res["keyboard_is_pressed"] = "ok"
except Exception as e:
    res["keyboard_is_pressed"] = repr(e)

try:
    import mss
    with mss.mss() as sct:
        shot = sct.grab({"left": 0, "top": 0, "width": 8, "height": 8})
    res["mss_grab"] = "ok %s" % (shot.size,)
except Exception as e:
    res["mss_grab"] = repr(e)

try:
    import pydirectinput
    res["pydirectinput"] = "ok FAILSAFE=%s" % getattr(pydirectinput, "FAILSAFE", "?")
except Exception as e:
    res["pydirectinput"] = repr(e)

res["bad"] = [k for k, v in res["mods"].items() if not v["ok"] and v["need"] == "required"]
res["optional_missing"] = [k for k, v in res["mods"].items()
                           if not v["ok"] and v["need"] == "optional"]

def _write_report(path, data):
    """写报告；目标文件被系统锁住（宿主机写保护）时退到带时间戳的另一个文件名"""
    import time as _t
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        alt = os.path.join(os.path.dirname(path),
                           "envcheck_%s.json" % _t.strftime("%Y%m%d_%H%M%S"))
        with open(alt, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return alt


OUT_REAL = _write_report(OUT, res)
try:
    # windowed exe 的 sys.stdout 可能是 None，print 会抛 AttributeError →
    # 被 dispatch 兜住变成 exit=3，看着像自检失败。这里吞掉，结果以 json 为准。
    print(json.dumps({"bad": res["bad"], "optional_missing": res["optional_missing"],
                      "out": OUT_REAL}, ensure_ascii=False), flush=True)
except Exception:
    pass
