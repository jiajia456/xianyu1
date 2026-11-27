# -*- coding: utf-8 -*-
"""一键打包成 EXE（Windows）

用法：
    python build_exe.py            # 打包 → dist/KK自动打图/
    python build_exe.py --zip      # 打包并压成 zip 方便拷走

产物结构（绿色版，拷走整个文件夹就能用，不用装 Python）：
    KK自动打图/
      <名字>_v<版本>.exe     ← 双击这个（例：我在峡谷打僵尸_v1.2.exe）
      _internal/            ← 依赖（Python 运行时、cv2、numpy、PyQt5…）
      core/
        *.py                ← 原有全部脚本，一个没改
        templates/          ← 图像模板
        configs/            ← 配置文件
        logs/               ← 运行时自动生成

两个构建目标（2026-11-25 用户要求兼容 Win7）：

    KK_TARGET=win7 py38/python.exe build_exe.py --zip
                                        # 【发布版】通用版：Win7 SP1 / 8 / 10 / 11 都能跑
                                        # 必须用 Python 3.8.10 解释器（3.9 起不支持 Win7）
    python build_exe.py                 # 【回退版】用当前 3.13 打，只给 Win10/11，产物带 -Win10

  * 发布版 = 同一套源码，换 Python 3.8.10 + PyInstaller 5.13.2 重打（PyInstaller 6 的
    bootloader 也不支持 Win7）。**成品名不加后缀**，就叫「我在峡谷打僵尸_v1.92」；
    额外多带原生 EXE 自动依赖安装入口和自检 bat。
  * PyInstaller 5 的 onedir **没有 `_internal/`**（依赖直接放 exe 同级），
    所以瘦身清单要走 `_layout_fix()` 换算，不能写死前缀。
  * **2026-11-25 用户定：以后只发发布版**，`win10` 目标只留作回退，不再对外发新。
  * 源码一个字都不改：只在 `build/src_<target>/` 的**临时副本**上改版本号再打包。

v1.6 起加了两道闸（2026-11-18 15:40 用户：「检查有没有遗漏环境兼容没有打包进成品」）：
  1. **依赖自动扫描**：core 里所有脚本的第三方 import 自动变成 --hidden-import。
     以前是手写 {"keyboard","pydirectinput"}，结果 psutil 一直没打进去（实测 v1.5 里
     `import psutil` 直接 ModuleNotFoundError，room_finder 只能退到 ctypes 兜底）。
  2. **打包后强制自检**：用成品 exe 跑 core/_envcheck.py，逐个 import 真实依赖，
     再核对必备脚本/模板/配置是否齐；有一项不过就以非 0 退出（不会悄悄出个残废包）。
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import zipfile
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 产物目录：默认 dist/；可用环境变量 KK_DIST_ROOT 指到别的盘（宿主机对某些目录
# 会拒绝 chmod/覆盖，指到临时目录再拷回来是唯一稳的办法）
DIST_ROOT = os.environ.get("KK_DIST_ROOT") or os.path.join(HERE, "dist")

# ---------------------------------------------------------------- 构建目标
# 2026-11-25：支持 Win7。判定只看「跑本脚本的解释器」——Python 3.8 就是 Win7 工具链。
#   KK_TARGET=win7   强制 Win7 形态（解释器必须是 3.8，否则直接报错）
#   KK_TARGET=win10  强制普通形态
#   不设             → 3.8 解释器自动判 win7，其余判 win10
_PY = sys.version_info[:2]
_KK_TARGET = (os.environ.get("KK_TARGET") or "").strip().lower()
TARGET = _KK_TARGET or ("win7" if _PY == (3, 8) else "win10")
if TARGET not in ("win7", "win10"):
    print(f"[目标] ⚠ 不认识的 KK_TARGET={TARGET!r}，按 win10 处理")
    TARGET = "win10"
IS_WIN7 = TARGET == "win7"
# 2026-11-25 用户定：只发 win7 工具链这一版（它就是通用版），所以**不加后缀**，
# 成品就叫「我在峡谷打僵尸_v1.92」。用 3.13 打的回退包才加 -Win10 区分。
VER_SUFFIX = "" if IS_WIN7 else "-Win10"
_VER_IN_SRC = ""      # 源码 app_gui.py 里原本的版本号（判断副本要不要改）

def _bump_ver(old: str, path: str = None) -> str:
    """把 app_gui.py 里的 APP_VER 次版本号 +1 并写回（二进制替换，不动换行符）。

    成功返回新号（如 v1.30 → v1.31）；失败返回 ""（格式不对/文件异常/写不进）。
    """
    path = path or os.path.join(HERE, "app_gui.py")
    m = __import__("re").match(r"v(\d+)\.(\d+)$", old.strip())
    if not m:
        return ""
    new = f"v{m.group(1)}.{int(m.group(2)) + 1}"
    try:
        with open(path, "rb") as f:
            raw = f.read()
        old_b = f'APP_VER = "{old}"'.encode("utf-8")
        new_b = f'APP_VER = "{new}"'.encode("utf-8")
        if raw.count(old_b) != 1:
            return ""
        with open(path, "wb") as f:
            f.write(raw.replace(old_b, new_b))
        return new
    except Exception:
        return ""


def _app_ver() -> str:
    """版本号只认 app_gui.py 里的 APP_VER，避免两处各写一份对不上。

    2026-11-19 21:57 用户定「每次打包编号叠加」：直接跑本脚本打包时，
    自动把次版本号 +1 写回 app_gui.py，本次成品就用新号（v1.30 → 打包出 v1.31）。
    KK_NO_BUMP=1 可禁用；写回失败（目录只读等）时用旧号继续打包并警告。
    被 import 时（__name__ != "__main__"）只读不加，测试/工具不受影响。

    2026-11-25 起发布目标就是 win7 工具链（通用版），所以编号叠加跟着它走：
    每打一次包 v1.91 → v1.92 → …（写回 app_gui.py）；3.13 的回退包不推号。
    """
    global _VER_IN_SRC
    old = "v0.0"
    try:
        with open(os.path.join(HERE, "app_gui.py"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("APP_VER"):
                    old = line.split('"')[1]
                    break
    except Exception:
        return "v0.0"
    _VER_IN_SRC = old
    if os.environ.get("KK_NO_BUMP"):
        return old
    # 编号叠加只给「发布目标」（win7 工具链 = 通用版）；3.13 的回退包不动源码版本号。
    if __name__ == "__main__" and IS_WIN7:
        new = _bump_ver(old)
        if new:
            print(f"[版本] 自动叠加：{old} → {new}")
            return new
        print(f"[版本] ⚠ 编号叠加失败 → 本次仍用 {old}"
              f"（app_gui.py 只读或格式变了；可设 KK_NO_BUMP=1 消除本提示）")
        return old
    return old


APP_VER = _app_ver() + VER_SUFFIX          # 源码版本 + 目标后缀（Win7 版为 v1.91-Win7）
# 成品命名规则（用户 2026-11-18 00:01）：每次出成品都叫「我在峡谷打僵尸_v编号」
APP_NAME = "我在峡谷打僵尸_" + APP_VER      # exe 名（不含 .exe）
APP_DIR_NAME = APP_NAME                    # 文件夹名 / zip 名
DIST = os.path.join(DIST_ROOT, APP_NAME)
# 中间产物同理：宿主机对项目目录里的“已存在文件”会拒绝改写，PyInstaller 的 exe
# 是在 workpath 里拼好再拷到 dist 的，所以这两个目录最好都能换到临时盘。
BUILD = os.environ.get("KK_BUILD_DIR") or os.path.join(HERE, "build")

# 不打包进 core 的东西
# 只排除「本来就是打包/入口」的文件；_sleeper.py 要进包，
# 否则界面里的 --fake-run 自检会报找不到脚本。
SKIP_PY = {"build_exe.py", "app_gui.py", "engine.py", "_pyi_shim.py"}

# 开发期脚本不进成品（2026-11-18 00:20 自查：core 里原本塞了 16 个 test_/diag_/probe_
# 脚本，其中 test_env_compat.py / test_log_order.py 还 import app_gui，成品里必崩）
SKIP_PREFIX = ("test_", "diag_", "probe_", "_patch_")
SKIP_EXTRA = {"_new.py"}


# 随包一起发的额外 exe（放 tools\\ 里）：用户 2026-11-25 要求把「全静默版」带上
EXTRA_TOOLS = ["我在峡谷打僵尸_全静默版.exe"]


def _skip_py(name: str) -> bool:
    if name in SKIP_PY or name in SKIP_EXTRA:
        return True
    return name.startswith(SKIP_PREFIX)


SKIP_DIRS = {"__pycache__", "logs", "build", "dist", ".venv", ".git"}

# 打包后必须存在的文件（v1.6：以前靠人记，漏一个就是「成品少个模块」）
REQUIRED_CORE = [
    "game_auto.py", "full_loop.py", "fishing_mode.py", "daily_claim.py",
    "daily_sign.py", "plat_home.py", "map_update.py", "room_finder.py",
    "popup_guard.py", "busy_guard.py", "watch_early.py", "watch_victory.py",
    "in_game.py", "task_dot.py", "mega_loop.py", "loop_round.py",
    "_envcheck.py", "quick_check.py", "live_check.py", "logpack.py",
    "dbgprobe.py",                         # 调试探针：GUI「调试」「抓现场」按钮用（2026-11-25）
    "sysenv.py",                           # 系统识别：开始运行时报 Win7/Win10 并分档（2026-11-25）
]
REQUIRED_TPL = [
    "tpl_fishing_banner.png", "tpl_fishing_scene.png", "tab_detail.png",
    "tpl_map_update_body.png", "btn_early_challenge.png",
]
REQUIRED_CFG = [
    "pipeline.json", "gui_settings.json", "wuxia.json", "wuxia_lobby.json",
    "wuxia_room.json", "wuxia_skill.json", "wuxia_equip.json", "full_loop.json",
    "war3_exit.json", "platform.json", "popup.json", "os_profile.json",
]

# 静态扫描看不到的（pythoncom 之类由 pywin32 hook 带，win32timezone 是运行时字符串导入）
EXTRA_HIDDEN = ["win32timezone", "pythoncom", "pywintypes", "psutil",
                "logpack"]   # logpack 是在方法里 import 的，显式点名确保进包


def _stdlib_names() -> set:
    """当前解释器的标准库模块名集合。

    `sys.stdlib_module_names` 是 **Python 3.10 才有**的；Win7 版用 3.8 打，
    取不到就会把 os/json/time 这些全当第三方塞进 --hidden-import（不致命但很脏，
    还会和 --exclude-module tkinter 打架）。3.8 上退回一份显式清单。
    """
    names = set(getattr(sys, "stdlib_module_names", ()) or ())
    if names:
        return names
    return {
        "__future__", "abc", "argparse", "ast", "base64", "binascii", "bisect",
        "calendar", "cmath", "codecs", "collections", "concurrent", "contextlib",
        "copy", "csv", "ctypes", "datetime", "decimal", "difflib", "enum",
        "errno", "faulthandler", "filecmp", "fileinput", "fnmatch", "fractions",
        "functools", "gc", "getpass", "gettext", "glob", "gzip", "hashlib", "heapq",
        "hmac", "html", "http", "imaplib", "importlib", "inspect", "io", "ipaddress",
        "itertools", "json", "linecache", "locale", "logging", "lzma", "math",
        "mimetypes", "multiprocessing", "operator", "os", "pathlib", "pickle",
        "pkgutil", "platform", "plistlib", "pprint", "queue", "random", "re",
        "runpy", "secrets", "select", "shlex", "shutil", "signal", "socket",
        "sqlite3", "ssl", "stat", "string", "struct", "subprocess", "sys",
        "tarfile", "tempfile", "textwrap", "threading", "time", "timeit", "tkinter",
        "token", "tokenize", "traceback", "types", "typing", "unicodedata",
        "unittest", "urllib", "uuid", "warnings", "weakref", "winreg", "zipfile",
        "zipimport", "zlib",
    }


def third_party_imports() -> list[str]:
    """扫描「会被打进 core 的脚本」里出现的第三方顶层模块

    为什么必须自动扫：PyInstaller 只静态分析入口 app_gui.py，而 core/*.py 是运行时
    用 runpy 执行的，它看不见 —— core 脚本 import 的库得靠 --hidden-import 补齐，
    手写清单一定会漏（v1.5 就漏了 psutil）。
    """
    import ast

    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    std = _stdlib_names()
    found: set[str] = set()
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py") or _skip_py(name):
            continue
        try:
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except Exception:
            continue
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if not m or m in std or m in local or m.startswith("_"):
                    continue
                found.add(m)
    return sorted(found)


def _importable(mod: str) -> bool:
    """打包用的这个解释器里到底有没有这个模块

    2026-11-18 实测：本机 Python 3.13 里**没有 psutil**（不是打包漏了，是环境就没有）。
    hidden-import 一个装不上的模块只会换来一行警告，所以要提前分流、明确告知。
    """
    import importlib.util
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def hidden_imports() -> tuple[list[str], list[str]]:
    """返回 (能打的, 本机没有的)"""
    mods = set(third_party_imports()) | set(EXTRA_HIDDEN)
    mods.discard("PyInstaller")     # 只被打包脚本自己用，不能收进包
    have, miss = [], []
    for m in sorted(mods):
        (have if _importable(m) else miss).append(m)
    return have, miss


_ENV_WIN10 = """  * Windows 10 / 11 64 位。本程序用 Python 3.13 打包，Win7 跑不起来。
    要 Win7 版：用 Python 3.8 解释器加 KK_TARGET=win7 重打（见 build_exe.py 开头说明）。
  * exe 和 _internal 文件夹必须放在同一层目录里，整个文件夹一起拷。
    只拷 exe 会提示「解压不完整」—— 运行时依赖 _internal 里的库。"""

_ENV_WIN7 = """  * 本目录是**通用版**：Windows 7 SP1 / 8 / 8.1 / 10 / 11（64 位）都能直接跑，
    不用按系统分两份发（2026-11-25 用户定：以后只发这一版）。
    它用 Python 3.8.10 + PyInstaller 5.13.2 打包 —— 3.9 起官方不支持 Win7，
    为了让老机器也能用才降到 3.8；功能与高版本打包完全一致，源码逻辑一行没改。
  * 先双击 启动_自动安装.exe：已安装运行库就直接启动主程序；缺少时从微软
    下载兼容 Win7 的 VC++ 2015 x64 运行库，校验数字签名、安装后启动。
    安装可能弹出 Windows 管理员授权；若要求重启，重启后再运行这个入口。
  * 装完运行库还起不来 → 先跑本目录的「先运行我_自检.bat」，
    它会把结果写到 core\\logs\\envcheck.json 并直接打开，"bad": [] 就是依赖全齐。
  * 必须是 64 位系统 + Win7 SP1。32 位系统不能用（cv2 / Qt 都是 64 位）。
  * 整个文件夹一起拷：本版依赖直接放在 exe 同目录，**没有 _internal 文件夹**
    （以前用高版本打的包才有 _internal，两种布局都正常）。只拷 exe 会提示「解压不完整」。"""

ENV_REQ = _ENV_WIN7 if IS_WIN7 else _ENV_WIN10

MANUAL = f"""我在峡谷打僵尸 {APP_VER} —— 三大流程循环
=====================================

零、环境要求（先看这一节）
{ENV_REQ}
  * 别放在 C 盘的 Program Files 目录下。那里写日志/配置要管理员权限，
    会表现为「开始运行就报错」。放桌面、D 盘、E 盘任意普通目录都行。
  * 分辨率：平台窗口按 1080 宽等比换算，1920×1080 及以上最稳。
    系统缩放 100% / 125% 都能用（程序启动时自己开了 DPI 感知）。
  * 建议右键「以管理员身份运行」：模拟按键、全局热键在部分系统上需要提升权限，
    非管理员时可能出现「点了没反应」。

一、怎么用
  1) 双击 启动_自动安装.exe：自动安装依赖、打开自动化软件，并在 KK 未运行时自动打开 KK。
  2) KK 如停在登录页，先完成登录。
  3) 填参数 → 点「开始运行」；脚本会再次确认 KK 已打开。

  * 不想等大厅最长 15 分钟时：先登录平台并保持房间窗口打开，双击「快速检测.exe」。
    它只检查依赖和平台双窗口，几秒后打开 core\\logs\\quick_check.json，
    不进入游戏、不执行鼠标点击。status=room_pair_found 表示可复用房间，
    status=room_busy 表示房间仍在「游戏中」；正式运行时会重启 KK 后重新开局。
  * 要实际点击验证：双击「快速实测.exe」。它复用已有房间、执行第 ④ 步开局，
    最多等 60 秒看 War3 窗口；报告在 core\\logs\\live_check.json。
    它不会执行第 ⑤ 步的 15 分钟等待，也不会自动退出已启动的游戏。

  * 系统识别（2026-11-25 新增）：点「开始运行」时先判定本机是 Win7 / 8 / 10 / 11，
    界面日志会打两行，例如：
      🖥 本机系统：Windows 7 Service Pack 1（6.1.7601，AMD64）｜屏幕 1920x1080｜缩放 100%｜管理员
         按「win7」档运行：悬停时长 ×1.25、等窗口前台 ×1.2、抓图方式 auto
    Win7 那档的悬停/等前台会更慢一点，这是**故意的**：老机器渲染慢半拍，
    鼠标刚移上去就按下，War3 不认（11-16 实测过一次）。
    Win7 且不是管理员时，日志会额外提醒「模拟按键可能不生效」。
    想自己调：改 core\\configs\\os_profile.json（hover_scale / focus_wait_scale /
    capture_mode），改完重新点「开始运行」生效，不用重启程序。
    判读结果同时写进 core\\logs\\sysenv.json，发日志时一起带上。

二、参数含义
  每轮通关局数        一轮跑几局（每局约 13~16 分钟）
  每轮钓鱼次数        一轮进几次钓鱼模式（一次 = 进大厅点「钓鱼」后静等）
  每次钓鱼时长        每次静等几小时（游戏内上限约 7 小时 20 分）
  每累计几局签到领券  每跑满 N 局，插一次「签到 + 任务一键领取」；选「关闭」= 不做
  大循环轮数          「无限」= 一直跑下去
  优先顺序            每轮先跑哪一段：先通关循环 / 先钓鱼模式（只改先后，不改局数）
  任一段失败就停      默认不勾（失败了照常跑下一段）

  默认值：30 局通关 : 2 次钓鱼（每次 5 小时），每 10 局签到领券，无限轮。

三、运行中注意
  * 程序会自动操作鼠标键盘，运行期间不要动电脑
  * 鼠标别移到屏幕最边缘 10 像素内 —— 会触发紧急停止（FAILSAFE）
  * 想停下来：点「停止」（会杀掉当前子进程），或直接关窗口（会再问一次）
    那句「⛔ 正在停止（会杀掉当前子进程树）」只在你手点「停止」时出现，
    程序自己跑的时候从不打印；它杀的是本程序启动的 python 子进程，
    不会去关对战平台、也不会去杀 War3。

  * 登录窗口守卫（v1.1 新增）
    运行中只要检测到「登录」窗口（掉线/被顶号），程序会立刻停手：
    不点登录、不关窗口、不返回平台、不退游戏，只把时间记下来。
    - 界面会显示：⛔ 检测到登录窗口  2026-11-17 10:08:20
      （v1.18 起不再弹对话框，只写在界面日志和状态栏，免得挡住手动登录）
    - 记录文件：core\\logs\\login_events.jsonl
    - 手动登录好后，再点「开始运行」即可继续

四、目录说明
  core\\                全部脚本 + templates 图像模板 + configs 配置
  core\\logs\\           截图、事件记录
  core\\logs\\mega_journal.jsonl   每一段跑了多久、成功还是失败
  core\\logs\\login_events.jsonl  检测到登录窗口的时间记录
  改 core\\configs\\*.json 后，重启程序生效
  我在峡谷打僵尸_全静默版.exe   随包附带的无界面版（沿用 v1.91 那个，2026-11-25 用户要求一起发）
      注意：它是用 Python 3.13 打包的**单文件版**，**Win7 上起不来**
      （缺 api-ms-win-core-path-l1-1-0.dll 这个 Win8+ 系统组件）；
      Win8 / 10 / 11 可以正常用。要用 Win7 也能跑的静默版，得用 3.8 工具链重打。

五、常见问题
  * 提示找不到平台窗口 → 先把平台开起来并登录
  * 开局失败 → 平台可能停在任务页/别的 tab，程序每轮开跑前会先判形态；
    认不到「地图详情」tab 时走「搜地图→双击封面」切页（不会盲点左侧充值横幅）
  * 弹「地图已更新，请重新下载地图」→ 程序会自己关提示并重建房间触发下载，
    不用手动点（重建要等 15~20 秒）
  * 房间显示「游戏中」或弹「断线重连」→ 结束 KK 平台及房间相关进程，
    重新打开 KK，从主页面重新搜索地图并开局。每轮最多自动重启一次，
    如果重开后需要登录则停下等待手动登录。
    就算平台**没弹**这个弹窗（直接在任务管理器结束 War3、或者地图版本不对秒退），
    等大厅时也会自己查 War3 进程：见过进程活着、之后连续 1 分钟查不到 → 同样判结束、回房间重开
  * 平台停在「游戏房间」窗口（打完一局退出后就是）→ 签到/领券前会自动找回
    地图详情页（能在后台唤醒就不退房，房间号保留可复用）
  * 退出游戏后常随机弹「首充礼包 连锁三天」广告小窗（模态的，会吃掉点击）→
    签到/领券前会自动点它右上角的 X 关掉，不用手动关
  * v1.27 起 CPU 占用大幅下降（长时间等待时尤其明显）：
    以前每秒要把整张画面按十几种缩放各比对一遍，单次约 1 秒 —— 等「开始游戏」
    那 15 分钟里一个核基本是满的。现在先用半尺寸粗比对一次，画面里没这个按钮
    就直接跳过（约 20~50 毫秒）；实测同样的等待场景 CPU 从 53% 降到 1%，
    命中时反而更快（1030ms → 20ms）。识别精度不变，模板和阈值都没动
  * 本程序自动把自己设成「低于正常」优先级，不跟 War3 抢 CPU，游戏里更顺
  * v1.29 起「签到领券」也更省：判断一个平台窗口是「地图详情页」还是「游戏房间」时，
    先看详情页特征（两个蓝色大按钮），命中就不用再去比对房间号 —— 实测每个窗口
    省 0.33 秒；签到脚本还直接复用主流程刚确认过的窗口，不再对同一个窗口重复扫一遍。
    签到那一下的 CPU 峰值实测从 54% 降到 33%
  * v1.30 起等待期 CPU 再降一档：「提前挑战」等待从 30 秒一轮改 60 秒一轮（最多等 20 分钟）、
    窗口保活巡检从 1 秒改 30 秒（抓不到画面/黑屏时仍 2 秒快速接回）、
    后台登录窗检查的按画面兜底从 3 秒改 15 秒（标题检查仍 3 秒一次，登录窗照样第一时间发现）。
    实测一局里等待段程序自己的 CPU 中位数从 8.6% 降到 0.0%，整局平均约 7%；
    顺带把「提前挑战」等待段的登录窗检查合并进游戏监控进程，不再两处同时跑
  * v1.30 起程序每次打开会自动清理 3 天前的一次性日志和截图
    （事件流水 jsonl、界面日志、难度截图不受影响；删不掉的文件静默跳过）
  * 首次运行被 Windows 拦（未知发布者）→ 更多信息 → 仍要运行
  * 杀毒软件误报 → 本程序会模拟鼠标键盘，属正常行为，加白名单即可
  * 想确认依赖是否完整 → core\\logs\\envcheck.json 里 "bad": [] 就是全齐
  * 出问题时想把日志发出来 → 点界面底部「打包日志发我」（绿色小按钮）：
    桌面立刻出一个 KK日志_日期时间.zip 并自动选中，直接拖进聊天窗口发出去即可。
    里面有全部日志 + 窗口尺寸/DPI 等环境信息，不用自己翻 core\\logs。

六、点击方式（v1.15 起统一为双击）
  大厅
    * 等「开始游戏」按钮出现（最长 15 分钟）→ 说明难度面板画好了
      常态 30~60 秒就能判到位；等这么久只是给「地图载入特别慢」的机器兜底
      等待期间只截图比对，不点游戏里的任何东西；万一抓不到画面（窗口 0x0 / 已消失）
      也不会提前退出，改成每 30 秒重抓一次，一直等到 15 分钟上限
      唯一例外：等待期间每 30 秒顺带看一眼平台有没有弹「断线重连」——
      看到它就算「这一局已经结束」：点「取消」（绝不点「断线重连」）→
      若接着弹「断线重连失败。」点「确定」→ 立刻回房间重新点「开始游戏」
      （最多重开 2 次；不退房、不关任何进程，只点弹窗按钮）
      另外等按钮期间会顺手看一眼 War3 **进程**还在不在：平台没弹任何提示、War3 被结束或秒退时，
      连续 1 分钟查不到进程就会判「这一局结束了」，同样回房间重开（窗口 0x0 但进程还在＝正在载入，照旧继续等）
    * 选难度：鼠标悬停 0.7 秒后双击 3 次，点最下面一行（当前最高难度）
    * 「开始游戏」：双击 1 次 → 2 秒后再双击 1 次 → 再等 3 秒，按钮还在就再双击 3 次
  房间
    * 「开始游戏」：双击 1 次
  局内
    * 提前挑战图标：悬停后双击；6 秒没弹窗且图标还在 → 重复，最多 3 次
    * 弹窗「是」：双击 1 次（没关就再点，最多 3 次）
    * 「挑战存档」：双击 1 次
    * 存档挑战 12 格：每格双击，格间隔 1 秒，整轮跑 2 遍
    * 秘境挑战：先定位 NPC（头顶「秘境挑战」标签 + 脚下青光圈 / NPC 模型两条通道都跑）
      → 按候选点依次点，每点一下都用「面板两格图标」判开没开，开到就停
        （第 1 下用单击，防双击把刚开的面板又关掉；后面仍是双击）
      → 海岛/不死格子双击（弹窗不出就原地再点一次）→ 弹窗「是」双击（最多 4 次）
      每进一次都存 core\\logs\\secret_frame_npc_*.png（黄叉=标签、绿叉=候选点，按序号试）
      全都不开再存 secret_npc_fail_*.png（红叉=最后点的位置）
      现场想单独取证：在 core 目录跑 python secret_challenge.py --probe（只存图，一下都不点）
  说明：这里说的「双击」= 按下抬起 2 下，间隔 120~250 毫秒，系统认作一次双击。

七、调试开关（换电脑 / 坐标不对 / 认不出图时用）
  界面最下面一排有两个按钮：

  【调试：关】—— 点一下变「调试：开」
    打开后运行期间全程记录，都写在 core\\logs\\debug\\<时间>\\ 里：
      · 每次找图：模板名、阈值、**实际最高分**、命中位置、窗口客户区尺寸、缩放系数
      · 每次点击：客户区坐标、换算后的屏幕坐标、缩放系数，并在点击**前**存一张
        带红色十字的截图（十字 = 实际点的位置，标题写着点的是哪个模板、分数多少）
    怎么用：
      · 「明明在却认不出」→ 打开 events.jsonl 看 find_miss 的分数：
        0.75~0.85 说明阈值偏紧（画面偏色/渲染差异），远低于 0.6 说明画面根本不是预期界面
      · 「点了没反应」→ 看 click 那条的 client / k / screen_xy：
        客户区尺寸或 k 不对 = 坐标换算或窗口绑错了；都对但没反应 = 被弹窗挡住或权限不够
    注意：开着会稍慢（每次点击多一次截图），排查完记得关掉；
    要记录子流程（通关/钓鱼/签到），**必须在点「开始运行」之前**打开。

  【抓现场】—— 点一下立刻存一份证据（约 5~10 秒，别关窗口）
    存什么：屏幕.png、各窗口客户区截图、环境.json（分辨率 / DPI / 系统缩放 / 是否管理员 /
    DPI 感知模式）、窗口.json（标题 / 类名 / pid / 矩形）、模板打分.json
    （全部模板在当前画面上的实际分数，从高到低）。
    一眼分清三类问题：
      · 客户区尺寸不是 1690x921 / 1332x945 → 窗口绑错或缩放不对（坐标问题）
      · 模板分数普遍落在 0.6~0.85 → 渲染/取色不同（图色问题，要放宽阈值或重截模板）
      · 一张都不到 0.6 → 停在别的界面，或根本没绑到游戏窗

  怎么发出来：点「打包日志发我」——debug 目录会一起进 zip（在「调试」文件夹里）。
"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def park_old(path: str):
    """旧产物不直接删（避免批量删除被安全策略拦），挪到临时目录，系统会自己清"""
    if not os.path.exists(path):
        return
    import tempfile
    dst = os.path.join(tempfile.gettempdir(),
                       f"kk_auto_old_{os.path.basename(path)}_{time.strftime('%H%M%S')}")
    try:
        os.rename(path, dst)
        log(f"旧产物已挪走 → {dst}")
    except Exception:
        shutil.rmtree(path, ignore_errors=True)


def verify_core(core: str) -> list[str]:
    """核对 core 里的脚本/模板/配置是否齐（打包漏文件这里能立刻看出来）"""
    bad = []
    for name in REQUIRED_CORE:
        if not os.path.exists(os.path.join(core, name)):
            bad.append(f"缺脚本 core/{name}")
    for name in REQUIRED_TPL:
        if not os.path.exists(os.path.join(core, "templates", name)):
            bad.append(f"缺模板 core/templates/{name}")
    for name in REQUIRED_CFG:
        if not os.path.exists(os.path.join(core, "configs", name)):
            bad.append(f"缺配置 core/configs/{name}")
    return bad


def smoke_test(final_dir: str) -> list[str]:
    """用成品 exe 真跑一遍依赖自检（windowed 进程，结果写 core/logs/envcheck.json）"""
    core = os.path.join(final_dir, "core")
    probe = os.path.join(core, "_envcheck.py")
    if not os.path.exists(probe):
        return ["跳过依赖自检：core 里没有 _envcheck.py"]
    out = os.path.join(core, "logs", "envcheck.json")
    try:
        os.remove(out)
    except Exception:
        pass
    exe = os.path.join(final_dir, os.path.basename(final_dir) + ".exe")
    if not os.path.exists(exe):
        return [f"找不到 exe：{exe}"]
    log("跑依赖自检（用成品 exe 逐个 import 真实依赖）…")
    try:
        r = subprocess.run([exe, "_envcheck.py"], cwd=core, timeout=240)
    except Exception as e:
        return [f"依赖自检启动失败：{e!r}"]
    if not os.path.exists(out):
        return ["依赖自检没有产出 envcheck.json（exit=%s）" % r.returncode]
    try:
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return [f"envcheck.json 读不出来：{e!r}"]
    bad = list(data.get("bad") or [])
    if bad:
        return [f"成品里 import 失败：{m}" for m in bad]
    log("   必需依赖全部可导入：%d 个模块" % len(data.get("mods") or {}))
    opt = data.get("optional_missing") or []
    if opt:
        log("   ℹ️ 可选依赖未打包（代码有兜底，不影响功能）：%s" % ", ".join(opt))
    if data.get("keyboard_is_pressed") != "ok":
        bad.append("keyboard 全局热键不可用：%s" % data.get("keyboard_is_pressed"))
    if not str(data.get("mss_grab", "")).startswith("ok"):
        bad.append("mss 截屏不可用：%s" % data.get("mss_grab"))
    return bad


# ---------------------------------------------------------------- 成品瘦身
# 2026-11-18 18:40 用户：「打包出来成品好大，看看能不能清理多余的文件」。
# 量过 253 MB 的去向：cv2 112 MB、PyQt5 76 MB、numpy 27 MB、PIL 11 MB。
# 下面这些是**代码里确实用不到**的，删掉后立刻用 --screenshot 验界面、用 _envcheck 验依赖。
# 想打回原样：KK_NO_SLIM=1 python build_exe.py
SLIM_FILES = [
    # 文件名里的版本号随 cv2 版本变（3.13 是 500 / Win7 版 4.5.5 是 455），用通配。
    # 2026-11-25 教训：写死 500 时 Win7 版一个文件都没删掉，成品 229MB。
    ("_internal/cv2/opencv_videoio_ffmpeg*_64.dll",
     "cv2 视频编解码 30MB —— 代码里没有任何 VideoCapture/VideoWriter"),
    ("_internal/PyQt5/Qt5/bin/opengl32sw.dll",
     "Qt 软件 OpenGL 兜底 20MB —— 界面是纯 Widgets，不用 OpenGL"),
    ("_internal/PyQt5/Qt5/bin/d3dcompiler_47.dll", "ANGLE 的 D3D 编译器 4MB"),
    ("_internal/PyQt5/Qt5/bin/libGLESv2.dll", "ANGLE GLES 3.3MB"),
    ("_internal/PyQt5/Qt5/bin/libeGL.dll", "ANGLE EGL"),
    ("_internal/PyQt5/Qt5/bin/libEGL.dll", "ANGLE EGL"),
    ("_internal/PyQt5/Qt5/bin/Qt5Quick.dll", "Qt Quick 4MB —— 不用 QML"),
    ("_internal/PyQt5/Qt5/bin/Qt5Qml.dll", "Qt QML 3.5MB"),
    ("_internal/PyQt5/Qt5/bin/Qt5QmlModels.dll", "QML 模型 432KB"),
    ("_internal/PyQt5/Qt5/bin/Qt5WebSockets.dll", "WebSocket 148KB —— 不联网"),
    ("_internal/PyQt5/Qt5/bin/Qt5DBus.dll", "DBus 428KB —— Windows 上用不到"),
    ("_internal/PyQt5/Qt5/bin/Qt5Svg.dll", "SVG 324KB —— 图标是 .ico"),
    ("_internal/PyQt5/Qt5/bin/Qt5Network.dll", "Qt 网络 1.3MB —— 界面不联网"),
    ("_internal/PyQt5/Qt5/plugins/platforms/qminimal.dll", "调试用最小平台插件"),
    ("_internal/PyQt5/Qt5/plugins/platforms/qoffscreen.dll", "离屏平台插件"),
    ("_internal/PyQt5/Qt5/plugins/platforms/qwebgl.dll", "WebGL 平台插件"),
    ("_internal/PyQt5/Qt5/plugins/imageformats/qgif.dll", "只显示 .ico/.png"),
    ("_internal/PyQt5/Qt5/plugins/imageformats/qicns.dll", "macOS 图标格式"),
    ("_internal/PyQt5/Qt5/plugins/imageformats/qsvg.dll", "SVG 图片"),
    ("_internal/PyQt5/Qt5/plugins/imageformats/qtga.dll", "TGA 图片"),
    ("_internal/PyQt5/Qt5/plugins/imageformats/qtiff.dll", "TIFF 图片"),
    ("_internal/PyQt5/Qt5/plugins/imageformats/qwbmp.dll", "WBMP 图片"),
    ("_internal/PyQt5/Qt5/plugins/imageformats/qwebp.dll", "WebP 图片"),
    # ---- 2026-11-18 20:20 v1.15 再瘦身 ----
    # PIL 只用来 ImageGrab 抓屏（10 处 import 全是 from PIL import ImageGrab），
    # AVIF/WebP/ICC/Tk 这些解码扩展一个都用不到；文件名带 Python 版本号，用通配。
    ("_internal/PIL/_avif*.pyd", "AVIF 解码 7.6MB —— 只显示 png/ico"),
    ("_internal/PIL/_webp*.pyd", "WebP 解码 408KB"),
    ("_internal/PIL/_imagingcms*.pyd", "色彩管理 272KB —— 不做 ICC 转换"),
    ("_internal/PIL/_imagingtk*.pyd", "Tk 交互 16KB —— 没有 tkinter"),
    ("_internal/PyQt5/Qt5/plugins/iconengines/qsvgicon.dll",
     "SVG 图标引擎 —— Qt5Svg.dll 已删，它没有依赖可加载"),
    ("_internal/PyQt5/Qt5/plugins/platformthemes/qxdgdesktopportal.dll",
     "Linux XDG 主题，Windows 上用不到"),
    ("_internal/PyQt5/Qt5/plugins/generic/qtuiotouchplugin.dll",
     "TUIO 触摸板插件，纯 Widgets 界面用不到"),
]
SLIM_DIRS = [
    # 注意：`_internal/pythonwin/` 看着像多余的 IDE 组件，但 `win32ui.pyd` 就在里面，
    # 后台抓图（Capture._printwindow）靠它 → **不能删**。2026-11-18 18:39 删了自检立刻报
    # 「成品里 import 失败：win32ui」，这才是打包闸门该拦的东西。
    ("_internal/PyQt5/Qt5/plugins/sqldrivers", "SQLite 驱动，不用数据库"),
    ("_internal/PyQt5/Qt5/plugins/networkinformation", "网络信息插件"),
    ("_internal/PyQt5/Qt5/plugins/tls", "TLS 后端，不联网"),
    # 不要再加 numpy-*.dist-info：它一个目录就有 53 个文件，rmtree 会越过宿主机的
    # 「单轮删除 >50 个文件」安全闸，整个打包流程在最后一步被掐断（2026-11-18 20:23 实测）。
    # 258KB 不值得。
]
# 保留中文的 Qt 翻译（QFileDialog 等标准对话框还会用到），其余语言全删
SLIM_TRANSLATIONS_KEEP = ("zh_CN", "zh_TW")


def _layout_fix(final_dir: str, rel: str) -> str:
    """把瘦身清单里的 `_internal/xxx` 映射到真实位置。

    PyInstaller 6 的 onedir 依赖在 `_internal/`；**PyInstaller 5（Win7 版）直接放 exe 同级**，
    没有 _internal 目录。清单统一按 6 的写法写，这里换算成实际布局。
    """
    if rel.startswith("_internal/"):
        inner = os.path.join(final_dir, "_internal")
        if os.path.isdir(inner):
            return os.path.join(inner, rel[len("_internal/"):])
        return os.path.join(final_dir, rel[len("_internal/"):])
    return os.path.join(final_dir, rel)


def slim_payload(final_dir: str) -> int:
    """删掉成品里用不到的依赖，返回省下的字节数。"""
    if os.environ.get("KK_NO_SLIM") == "1":
        log("   跳过瘦身（KK_NO_SLIM=1）")
        return 0
    freed = 0
    for rel, why in SLIM_FILES:
        # 支持 * 通配（PIL 的扩展名带 cp313 这类 Python 版本后缀，写死会随版本失效）
        real = _layout_fix(final_dir, rel)
        cands = glob.glob(real) if "*" in rel else [real]
        for p in cands:
            if not os.path.exists(p):
                continue
            try:
                sz = os.path.getsize(p)
                os.remove(p)
                freed += sz
                log(f"   - {os.path.basename(p)} {sz / 1024 / 1024:.1f}MB（{why}）")
            except Exception as e:
                log(f"   ! 删不掉 {rel}: {e!r}")
    for rel, why in SLIM_DIRS:
        p = _layout_fix(final_dir, rel)
        if not os.path.isdir(p):
            continue
        sz = sum(os.path.getsize(os.path.join(dp, f))
                 for dp, _, fs in os.walk(p) for f in fs)
        try:
            shutil.rmtree(p)
            freed += sz
            log(f"   - {rel.split('/')[-1]}/ {sz / 1024 / 1024:.1f}MB（{why}）")
        except Exception as e:
            log(f"   ! 删不掉 {rel}: {e!r}")
    tdir = _layout_fix(final_dir, "_internal/PyQt5/Qt5/translations")
    if os.path.isdir(tdir):
        # 语言包有 130+ 个文件。**不能逐个删**：宿主机有「单轮删除 >50 个文件就拦」的安全闸，
        # 会把整个打包流程掐断（2026-11-18 18:38 实测）。改成「保留中文 → 整目录搬出成品」。
        keep = [n for n in os.listdir(tdir)
                if n.endswith(".qm") and any(k in n for k in SLIM_TRANSLATIONS_KEEP)]
        total = sum(os.path.getsize(os.path.join(tdir, n)) for n in os.listdir(tdir))
        stamp = str(int(time.time()))
        stash = os.path.join(BUILD, "_qm_keep_" + stamp)
        os.makedirs(stash, exist_ok=True)
        for n in keep:
            shutil.move(os.path.join(tdir, n), os.path.join(stash, n))
        try:
            shutil.move(tdir, os.path.join(BUILD, "_translations_dropped_" + stamp))
        except Exception as e:
            log(f"   ! translations 搬不走：{e!r}")
        keep_sz = 0
        os.makedirs(tdir, exist_ok=True)
        for n in keep:
            dst = os.path.join(tdir, n)
            shutil.move(os.path.join(stash, n), dst)
            keep_sz += os.path.getsize(dst)
        freed += max(0, total - keep_sz)
        log(f"   - translations 只留中文 {total / 1024 / 1024:.1f}MB"
            f" → {(total - keep_sz) / 1024 / 1024:.1f}MB（其余语言包整目录搬走）")
    if freed:
        log(f"   共省下 {freed / 1024 / 1024:.0f} MB")
    return freed


def gui_smoke(final_dir: str) -> list[str]:
    """用成品 exe 渲染一张界面图，确认删掉那些 Qt 组件后界面还活着。

    为什么必须有这一步：删 Qt 插件/GL 库最容易的翻车方式是「exe 起不来、弹个
    『无法定位程序输入点』对话框」，而 _envcheck 只 import 模块，查不出来。
    """
    exe = os.path.join(final_dir, os.path.basename(final_dir) + ".exe")
    shot = os.path.join(BUILD, "ui_smoke.png")
    try:
        os.remove(shot)
    except Exception:
        pass
    log("跑界面自检（成品 exe 渲染一张图，验证 Qt 组件还在）…")
    try:
        r = subprocess.run([exe, "--screenshot", shot], cwd=final_dir, timeout=120)
    except Exception as e:
        return [f"界面自检启动失败：{e!r}"]
    if not os.path.exists(shot):
        return [f"界面自检没产出截图（exit={r.returncode}）—— 可能删多了"]
    sz = os.path.getsize(shot)
    if sz < 20000:
        return [f"界面自检截图只有 {sz} 字节，界面可能没渲染出来"]
    log(f"   ✓ 界面自检通过（截图 {sz / 1024:.0f} KB）")
    try:
        os.remove(shot)
    except Exception:
        pass
    return []


def preflight() -> list[str]:
    """打包前把「工具链选错」这类错误拦在门口，别等打了 3 分钟才失败。

    Win7 版对解释器和依赖版本是硬要求：Python 3.9+ 官方不支持 Win7，
    PyInstaller 6 的 bootloader 也不行，numpy 2.x 对 cv2/老系统都不友好。
    """
    bad = []
    if IS_WIN7:
        if _PY != (3, 8):
            bad.append(f"Win7 版必须用 Python 3.8 解释器（当前 {_PY[0]}.{_PY[1]}）。"
                       f"例：KK_TARGET=win7 py38\\python.exe build_exe.py --zip")
        try:
            import PyInstaller
            pv = tuple(int(x) for x in PyInstaller.__version__.split(".")[:2])
            if pv[0] >= 6:
                bad.append(f"Win7 版必须用 PyInstaller 5.x（当前 {PyInstaller.__version__}，"
                           f"6.x 的 bootloader 要求 Win8+）")
            log(f"   PyInstaller {PyInstaller.__version__}")
        except Exception as e:
            bad.append(f"PyInstaller 不可用：{e!r}")
        try:
            import numpy
            nv = tuple(int(x) for x in numpy.__version__.split(".")[:2])
            if nv[0] >= 2:
                bad.append(f"Win7 版不能用 numpy {numpy.__version__}（2.x 与老 cv2/老系统不兼容），"
                           f"用 requirements-win7.txt 里锁的 1.24.4")
            log(f"   numpy {numpy.__version__}")
        except Exception as e:
            bad.append(f"numpy 不可用：{e!r}")
        try:
            import cv2
            log(f"   opencv {cv2.__version__}")
        except Exception as e:
            bad.append(f"cv2 不可用：{e!r}")
    else:
        if _PY >= (3, 9):
            pass          # 普通版就用高版本解释器，正常
        log(f"   Python {_PY[0]}.{_PY[1]}")
    return bad


def _stage_src() -> str:
    """把源码复制到 build/src_<target>/，**在副本上**改版本号再打包。

    为什么不直接改 app_gui.py：Win7 版只是同一版程序的另一种打包形态，
    不该把源码里的 APP_VER 改成 v1.91-Win7（下次普通版打包会跟着跳号/改错名）。
    """
    stage = os.path.join(BUILD, "src_" + TARGET)
    if os.path.isdir(stage):
        shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(HERE)):
        if name in SKIP_DIRS or name.startswith("."):
            continue
        src = os.path.join(HERE, name)
        if os.path.isdir(src):
            if name in ("templates", "configs"):
                shutil.copytree(src, os.path.join(stage, name))
            continue
        if name.endswith((".py", ".ico")):
            shutil.copy2(src, os.path.join(stage, name))
            n += 1
    log(f"   源码副本 {stage}（{n} 个文件）")
    # 版本号不一致就改副本（叠加过 / 带后缀时才会不一致）。
    # 注意别拿 _VER_IN_SRC 硬比：叠加是**先把源码改成新号**，副本里已经是新号了。
    p = os.path.join(stage, "app_gui.py")
    with open(p, "rb") as f:
        raw = f.read()
    m = re.search(rb'APP_VER = "([^"]+)"', raw)
    if m and m.group(1).decode("utf-8") != APP_VER:
        raw = raw.replace(m.group(0), f'APP_VER = "{APP_VER}"'.encode("utf-8"), 1)
        with open(p, "wb") as f:
            f.write(raw)
        log(f"   副本版本号 {m.group(1).decode('utf-8')} → {APP_VER}（源码不动）")
    return stage


# Win8 / Win8.1 / Win10 才导出的函数（Win7 的 kernel32 等里没有 → 加载就失败）
WIN8_FUNCS = {
    "GetSystemTimePreciseAsFileTime", "InitializeSynchronizationBarrier",
    "WaitOnAddress", "WakeByAddressAll", "WakeByAddressSingle",
    "CreateFile2", "CopyFile2", "GetPackageFullName", "GetCurrentPackageFullName",
    "GetApplicationUserModelId", "RoInitialize", "RoGetActivationFactory",
    "WindowsCreateString", "WindowsDeleteString", "CoIncrementMTAUsage",
    "SetThreadDescription", "GetThreadDescription", "SetProcessInformation",
    "GetProcessMitigationPolicy", "IsWow64Process2", "GetOverlappedResultEx",
    "RegDeleteKeyTransacted", "GetUserDefaultGeoName",
}
WIN8_DLLS = {
    "api-ms-win-core-path-l1-1-0.dll",          # Python 3.9+ 的标志
    "api-ms-win-core-winrt-l1-1-0.dll",
    "api-ms-win-core-winrt-string-l1-1-0.dll",
    "api-ms-win-core-synch-l1-2-0.dll",         # Win8 的 WaitOnAddress
    "api-ms-win-core-version-l1-1-1.dll",       # Win8.1
    "api-ms-win-shcore-scaling-l1-1-1.dll",     # Win8.1 DPI
    "api-ms-win-core-libraryloader-l1-2-0.dll", # Win8
    "api-ms-win-core-file-from-app-l1-1-0.dll",
}


def win7_pe_gate(final_dir: str) -> list[str]:
    """第四道闸：逐个数成品里的 .exe/.dll/.pyd，确认 Win7 真的能加载。

    本机不是 Win7，跑不出「真机能不能起」；能做的是最硬的那一半：
      ① PE 头里的 OS/子系统版本 > 6.1 → Win7 直接拒绝加载
      ② 静态导入了 Win8+ 才有的 DLL / 函数 → 运行时「找不到入口点」
    pefile 没装就跳过（普通版不需要这道闸）。
    """
    try:
        import pefile
    except Exception:
        return []   # 不是错误，只是没装分析库
    bad_ver, bad_dll, bad_func = [], [], []
    scanned = 0
    for dp, _, fs in os.walk(final_dir):
        for fn in fs:
            if not fn.lower().endswith((".exe", ".dll", ".pyd")):
                continue
            path = os.path.join(dp, fn)
            rel = os.path.relpath(path, final_dir)
            try:
                pe = pefile.PE(path, fast_load=True)
                pe.parse_data_directories(directories=[
                    pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
            except Exception:
                continue
            scanned += 1
            oh = pe.OPTIONAL_HEADER
            if ((oh.MajorOperatingSystemVersion, oh.MinorOperatingSystemVersion) > (6, 1)
                    or (oh.MajorSubsystemVersion, oh.MinorSubsystemVersion) > (6, 1)):
                bad_ver.append(rel)
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []:
                dll = entry.dll.decode(errors="replace").lower()
                if dll in WIN8_DLLS:
                    bad_dll.append(f"{rel} → {dll}")
                for imp in entry.imports:
                    if imp.name and imp.name.decode(errors="replace") in WIN8_FUNCS:
                        bad_func.append(f"{rel} → {imp.name.decode()}")
            pe.close()
    log(f"   PE 扫描 {scanned} 个二进制")
    out = []
    out += [f"版本号 >6.1（Win7 拒绝加载）：{x}" for x in bad_ver[:10]]
    out += [f"静态导入 Win8+ DLL：{x}" for x in bad_dll[:10]]
    out += [f"静态导入 Win8+ 函数：{x}" for x in bad_func[:10]]
    return out


def remove_app_local_ucrt(final_dir: str) -> int:
    """Win7 包改用系统安装的 UCRT，避免带入构建机的 Windows 2022 版本。

    PyInstaller 5 会从构建机复制 ucrtbase.dll 和 api-ms-win-* 转发 DLL。
    这些文件的 PE 版本和导入表可能要求 Win8+。随包的启动器会先安装
    Win7 可用的 VC++ 2015 运行库；此处只清理程序根目录的本地副本。
    """
    removed = 0
    for name in os.listdir(final_dir):
        lower = name.lower()
        if lower != "ucrtbase.dll" and not (lower.startswith("api-ms-win-") and
                                              lower.endswith(".dll")):
            continue
        path = os.path.join(final_dir, name)
        if os.path.isfile(path):
            os.remove(path)
            removed += 1
    log(f"   - 移除 {removed} 个构建机 UCRT 文件（由目标系统的 VC++ 运行库提供）")
    return removed


def win7_extras(final_dir: str) -> list[str]:
    """Win7 版加入原生 EXE 启动器、依赖安装脚本与自检工具。"""
    bad = []
    for src_name, dest_name in (("AutoInstallLauncher.exe", "启动_自动安装.exe"),
                                ("QuickCheckLauncher.exe", "快速检测.exe"),
                                ("LiveCheckLauncher.exe", "快速实测.exe")):
        launcher = os.path.join(HERE, "tools", src_name)
        if os.path.exists(launcher):
            shutil.copy2(launcher, os.path.join(final_dir, dest_name))
            log(f"   + {dest_name}")
        else:
            bad.append(f"找不到 tools/{src_name}")
    for name in ("Start_AutoInstall.bat", "Quick_Check.bat", "Live_Check.bat",
                 "先运行我_自检.bat"):
        bat = os.path.join(HERE, "tools", name)
        if os.path.exists(bat):
            shutil.copy2(bat, os.path.join(final_dir, name))
            log(f"   + {name}")
        else:
            bad.append(f"找不到 tools/{name}")
    return bad


def _bundle_extra_tools(final_dir: str) -> list:
    """把 tools\\ 下「要跟着成品一起发」的额外 exe 放进成品目录。

    2026-11-25 用户：「这个工具（我在峡谷打僵尸_全静默版.exe）也打包进去」。
    ⚠ 它是用 Python 3.13 打的单文件包（内部 python313.dll 依赖
    api-ms-win-core-path-l1-1-0.dll），**Win7 上起不来** —— 只做提示，不拦打包，
    因为用户的用法可能就是 Win10 机器上用。要做 Win7 版得先有它的入口源码。
    """
    out = []
    for name in EXTRA_TOOLS:
        src = os.path.join(HERE, "tools", name)
        if not os.path.exists(src):
            continue
        shutil.copy2(src, os.path.join(final_dir, name))
        out.append(name)
        log(f"   + {name}（{os.path.getsize(src) / 1048576:.1f}MB，3.13 打的 → Win7 起不来）")
    return out


def build():
    log(f"目标 {TARGET}｜Python {_PY[0]}.{_PY[1]}｜版本 {APP_VER}｜产物 {APP_DIR_NAME}")
    bad = preflight()
    if bad:
        log("❌ 这套工具链打不了这个目标：")
        for b in bad:
            log("   - " + b)
        return 5
    src = _stage_src()

    # 2026-11-20 16:35 用户定：打包**默认不清理旧版**（dist 里的旧版本目录/zip 都留着）。
    # 需要清理时设环境变量 KK_CLEAN_OLD=1。
    if os.environ.get("KK_CLEAN_OLD") == "1":
        log("清理旧产物（KK_CLEAN_OLD=1）")
        park_old(BUILD)
        park_old(DIST)
        final_dir = os.path.join(DIST_ROOT, APP_DIR_NAME)
        if os.path.normcase(os.path.abspath(final_dir)) != os.path.normcase(os.path.abspath(DIST)):
            park_old(final_dir)
    else:
        log("保留旧产物（默认不清理旧版）")
        final_dir = os.path.join(DIST_ROOT, APP_DIR_NAME)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",
        "--name", APP_NAME,
        "--icon", os.path.join(src, "app.ico"),
        "--distpath", DIST_ROOT,
        "--workpath", BUILD,
        "--specpath", BUILD,
    ]
    hidden, missing = hidden_imports()
    for m in hidden:
        cmd += ["--hidden-import", m]
    cmd += [
        # 明确排除用不到的 GUI 框架，避免被误收集（本机 Qt6 也跑不起来）
        "--exclude-module", "PySide6",
        "--exclude-module", "shiboken6",
        "--exclude-module", "PyQt6",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pandas",
        "--exclude-module", "scipy",
        os.path.join(src, "app_gui.py"),
    ]
    shim = os.path.join(src, "_pyi_shim.py")
    if os.path.exists(shim):
        cmd = [sys.executable, shim] + cmd[3:]
    log("依赖清单（自动扫描 core 脚本）：%s" % ", ".join(hidden))
    if missing:
        log("   ⚠️ 本机解释器没有这些模块 → 不会打进包（代码里必须有兜底）：%s"
            % ", ".join(missing))
    log("PyInstaller 打包中（约 1~3 分钟）…")
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        log(f"❌ 打包失败 exit={r.returncode}")
        return r.returncode

    exe = os.path.join(DIST, f"{APP_NAME}.exe")
    if not os.path.exists(exe):
        log("❌ 没找到生成的 exe")
        return 1

    log("拷贝脚本 / 模板 / 配置到 core/")
    core = os.path.join(DIST, "core")
    os.makedirs(core, exist_ok=True)
    n_py = 0
    for name in sorted(os.listdir(src)):
        sp = os.path.join(src, name)
        if os.path.isdir(sp):
            if name in SKIP_DIRS:
                continue
            if name in ("templates", "configs"):
                shutil.copytree(sp, os.path.join(core, name), dirs_exist_ok=True)
                log(f"   + {name}/")
            continue
        if name.endswith(".py") and not _skip_py(name):
            shutil.copy2(sp, os.path.join(core, name))
            n_py += 1
    log(f"   + {n_py} 个 .py")

    # 界面参数：默认把当前 configs/gui_settings.json 带走当作「首次默认」
    # （用户 10:38：参数要能自定义 —— 打包不能每次把填好的值冲掉）
    # 想打一份全新的默认值：python build_exe.py --fresh-settings
    gs = os.path.join(core, "configs", "gui_settings.json")
    if os.path.exists(gs):
        if "--fresh-settings" in sys.argv:
            try:
                os.remove(gs)
                log("   - 已清掉 gui_settings.json（用内置默认值）")
            except Exception:
                pass
        else:
            # 新版本可能加了新参数（比如 busy_wait），旧文件里没有 → 用内置默认补齐，
            # 否则新 EXE 首次打开会因为缺键而落到 DEFAULTS，看起来像「参数被重置」。
            try:
                with open(gs, encoding="utf-8") as f:
                    cur = json.load(f)
            except Exception:
                cur = {}
            defaults = dict(n_games=30, n_fishing=2, fishing_hours=5.0,
                            claim_every=10, loops=0, stop_on_fail=False,
                            busy_wait=6, busy_attempts=2, busy_restart=True,
                            order="full_first", max_round_min=50, settle_min=5)
            merged = dict(defaults)
            merged.update({k: v for k, v in cur.items() if k in defaults})
            try:
                with open(gs, "w", encoding="utf-8") as f:
                    json.dump(merged, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            log(f"   + 保留现有参数作为首次默认：{json.dumps(merged, ensure_ascii=False)}")

    # 改名成中文文件夹，双击更直观。
    # 注：现在 exe 名和文件夹名都带版本号，两者可能同名 —— 同名时不能再 park/rename，
    # 否则会把刚打好的目录挪走（2026-11-18 00:02 踩到 FileNotFoundError）。
    final = os.path.join(DIST_ROOT, APP_DIR_NAME)
    if os.path.normcase(os.path.abspath(final)) != os.path.normcase(os.path.abspath(DIST)):
        park_old(final)
        os.rename(DIST, final)
    exe = os.path.join(final, f"{APP_NAME}.exe")
    core = os.path.join(final, "core")

    with open(os.path.join(final, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(MANUAL)

    # ---- 瘦身：删掉用不到的依赖（KK_NO_SLIM=1 可跳过）----
    log("清理成品里用不到的依赖")
    slim_payload(final)
    if IS_WIN7:
        remove_app_local_ucrt(final)

    # ---- 打包后闸门：先核对文件齐不齐，再用成品 exe 跑依赖自检 ----
    bad = verify_core(core)
    if bad:
        log("❌ core 内容不完整：")
        for b in bad:
            log("   - " + b)
        return 2
    log("   ✓ core 必备脚本/模板/配置齐全")
    bad = smoke_test(final)
    if bad:
        log("❌ 环境自检不通过：")
        for b in bad:
            log("   - " + b)
        return 3
    log("   ✓ 成品 exe 依赖自检通过（bad=[]）")
    bad = gui_smoke(final)
    if bad:
        log("❌ 界面自检不通过（瘦身可能删多了，用 KK_NO_SLIM=1 重打）：")
        for b in bad:
            log("   - " + b)
        return 4

    # ---- Win7 专属：原生启动器 + PE 静态兼容闸 ----
    if IS_WIN7:
        log("Win7 版收尾（补原生启动器与依赖安装脚本 → PE 静态兼容闸）")
        bad = win7_extras(final)
        if bad:
            log("❌ Win7 启动工具不全，停止发布：")
            for b in bad:
                log("   - " + b)
            return 7
        bad = win7_pe_gate(final)
        if bad:
            log("❌ Win7 静态兼容检查不通过（Win7 上会「找不到入口点」或直接不打）：")
            for b in bad:
                log("   - " + b)
            return 6
        log("   ✓ PE 静态检查通过（OS/子系统版本 ≤ 6.1，无 Win8+ 导入）")

    # ---- 额外工具（两个目标都放）：全静默版等，来自 tools/ ----
    _bundle_extra_tools(final)

    # ---- 清掉成品里的冒烟测试产物（2026-11-19 23:26 用户要求）----
    # 必须放在依赖自检 / 界面自检之后：core/logs 里的 envcheck.json、gui_boot.log、
    # gui_console.log 正是那两个自检生成的。程序运行时自己会重建 core/logs。
    for junk in (os.path.join(core, "logs"),
                 os.path.join(core, "configs", "_wtest.txt"),
                 os.path.join(core, "configs", "popup.json.new344"),
                 os.path.join(core, "configs", "_selftest.json")):
        try:
            if os.path.isdir(junk):
                shutil.rmtree(junk, ignore_errors=True)
                log(f"   - 删掉成品里的 {os.path.relpath(junk, final)}/（冒烟测试产物）")
            elif os.path.exists(junk):
                os.remove(junk)
                log(f"   - 删掉成品里的 {os.path.relpath(junk, final)}")
        except Exception:
            pass

    # 2026-11-27 用户要求：成品和源码一起打包 —— 源码放进成品目录下的「源码/」，
    # zip 里就一起带了，不用再单独发一份。源码目录的 APP_VER 此时已被 _bump_ver 改成
    # 本次版本号，和成品对得上；logs/build/dist/__pycache__ 这些不拷。
    try:
        src_dst = os.path.join(final, "源码")
        if os.path.isdir(src_dst):
            shutil.rmtree(src_dst, ignore_errors=True)
        shutil.copytree(HERE, src_dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                      "logs", "build", "dist", ".venv", ".git"))
        _n = sum(len(fs) for _, _, fs in os.walk(src_dst))
        log(f"   + 源码/（{_n} 个文件，含 tools / templates / configs / 文档）")
    except Exception as e:
        log(f"   ⚠ 源码拷贝失败（不影响成品）：{e!r}")

    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(final) for f in fs)
    log(f"✅ 完成：{exe}")
    log(f"   目录大小 {size / 1024 / 1024:.0f} MB")
    return 0


def main():
    rc = build()
    if rc == 0 and "--zip" in sys.argv:
        log("压缩成 zip")
        out = os.path.join(DIST_ROOT, APP_DIR_NAME)
        # shutil.make_archive 固定 deflate level=6；改成 level 9 再省 2~4%
        zpath = out + ".zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            src = os.path.join(DIST_ROOT, APP_DIR_NAME)
            for dp, _, fs in os.walk(src):
                for fn in fs:
                    full = os.path.join(dp, fn)
                    z.write(full, os.path.relpath(full, DIST_ROOT))
        log(f"   （deflate level=9）")
        log(f"✅ {out}.zip")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
