# -*- coding: utf-8 -*-
"""KK 自动打图 · 三大流程循环 —— 图形界面（PyQt5）

打包后就是 KKAuto.exe：
  - 双击 → 打开这个界面
  - 被当作子进程调用（第一个参数是 .py 文件）→ 用内置解释器直接跑那个脚本，
    这样 full_loop.py / game_auto.py 里 `sys.executable + xxx.py` 的老写法打包后照样能用
"""
from __future__ import annotations

import functools
import io
import json
import os
import runpy
import subprocess
import sys
import threading
import time
import traceback

import engine

APP_TITLE = "我在峡谷打僵尸 · 三大流程循环"
APP_VER = "v2.22"

# 界面里提到「exe 文件名」的地方用这个，改名后文案不会说错（打包后取真实 exe 名）
EXE_LABEL = (os.path.basename(sys.executable) if getattr(sys, "frozen", False)
             else "app_gui.py")

# ---------------------------------------------------------------- 环境适配（DPI）
# 2026-11-17 18:52 用户问「换电脑的兼容问题有没有写进成品」。这一块就是答案的核心：
# Windows 的 DPI 感知必须在**进程第一步**定下来。等 Qt 建过窗口、屏幕上出现过窗口之后
# 再调 SetProcessDpiAwareness，系统直接返回 E_ACCESSDENIED（静默无效，不报错）。
# 原来只有 game_auto.main() 里设在调用，而 EXE 是 app_gui 先起 Qt → 那些调用全是空转，
# 结果在 125%/150% 缩放的笔记本上截图被系统「虚拟化」放大过，模板匹配分数明显下降。
# 这里放在模块最顶层（此时 PyQt5 还没被 import），确保是进程里第一个动 DPI 的。
def _setup_dpi() -> str:
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor（Win8.1+）
            return "per-monitor"
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()        # system（Win7）
            return "system"
    except Exception as e:
        return f"failed:{e!r}"


DPI_MODE = _setup_dpi()

# 日志区保留多少行。倒序显示（最新在最上面）时，Qt 自带的 setMaximumBlockCount
# 是从文档开头裁的 —— 那裁掉的正好是最新的行，所以改成 0（不限）+ 自己裁末尾。
LOG_MAX_LINES = 6000


def insert_block_at_top(edit, text: str, max_lines: int = LOG_MAX_LINES) -> bool:
    """把一段文本作为**一整块**插到日志控件最顶部（最新在最上面）

    用户 2026-11-17 20:05 要求：「信息最新排第一」——整个日志区倒序。
    - 一次传多行（"a\\nb"）时块内顺序保持不变，只有块与块之间是新的在上面。
    - 用户本来停在最上面才自动跟随；往下翻看旧日志时不会把他拽回去。
    - 超过 max_lines 时从**末尾**裁（倒序下末尾是最旧的）。

    返回是否发生了自动跟随（便于自检）。
    """
    from PyQt5.QtGui import QTextCursor

    bar = edit.verticalScrollBar()
    follow = bar.value() <= bar.minimum() + 2
    cur = edit.textCursor()
    cur.movePosition(QTextCursor.Start)
    edit.setTextCursor(cur)
    edit.insertPlainText(text + "\n")

    doc = edit.document()
    if doc.blockCount() > max_lines:
        cut = doc.findBlockByNumber(max_lines).position()
        if cut > 0:
            cut -= 1            # 连它前面那个换行一起删，末尾不留空行
        c2 = QTextCursor(doc)
        c2.setPosition(cut)
        c2.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        c2.removeSelectedText()

    if follow:
        bar.setValue(bar.minimum())
    return follow


def env_report() -> dict:
    """把「换一台电脑才会暴露」的环境参数一次性记下来，排障只看 gui_boot.log 就够"""
    out = {"dpi_mode": DPI_MODE}
    try:
        import ctypes
        import platform
        u = ctypes.windll.user32
        try:
            out["scale"] = round(int(u.GetDpiForSystem()) / 96.0, 2)
        except Exception:
            hdc = u.GetDC(0)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))   # LOGPIXELSX
            u.ReleaseDC(0, hdc)
            out["scale"] = round(dpi / 96.0, 2)
        out["phys_screen"] = f"{u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)}"
        out["monitors"] = int(u.GetSystemMetrics(80))
        out["admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        out["win"] = platform.version()
    except Exception as e:
        out["env_err"] = repr(e)
    return out

# ---------------------------------------------------------------- 崩溃兜底
# 背景（2026-11-17 16:42 用户反馈）：别的电脑上「点开始执行软件就关了」。
# 打成窗口程序后 stderr 是 devnull，PyQt5 又会在「槽函数抛异常」时直接 abort 进程，
# 用户那边什么提示都看不到。这里做三件事：
#   1) 全局 excepthook + 槽函数 @safe 兜底 → 异常只记日志，不再让进程退出
#   2) logs/gui_console.log 镜像界面日志（窗口没了也留证据）


def diag_dir() -> str:
    """放诊断日志的目录；core/logs 不可写（只读目录/被杀软锁）时退到 %APPDATA%"""
    d = engine.logs_dir()
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, "_w.tmp")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return d
    except Exception:
        alt = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                           "KKAuto", "logs")
        try:
            os.makedirs(alt, exist_ok=True)
        except Exception:
            pass
        return alt


LOG_CLEAN_DAYS = 3        # 2026-11-19 21:05 用户定：启动时清理 3 天前的一次性日志


def cleanup_old_logs(days: int = LOG_CLEAN_DAYS, root=None) -> tuple:
    """启动时清掉 N 天前的一次性日志/截图（只扫 logs 根目录，不进子目录）。

    清：*.log（bot_ / early_watch_ / gui_boot 等任务日志）+ 根目录的 .png
        （签到/领券/钓鱼等一次性现场截图）。
    留：gui_console.log（本进程正在写）；secret_frame_*.png（秘境自己的
        keep=4 滑窗管，不跟它打架）；*.json / *.jsonl（事件流水，单文件几 KB，
        排障要历史）；diff_rows/ 子目录（固定文件名每局覆盖，不堆积）。
    删不动的文件（宿主机写保护）静默跳过，绝不影响启动。
    返回 (删除个数, 释放字节)。
    """
    if root is None:
        root = diag_dir()
    if not os.path.isdir(root):
        return (0, 0)
    cutoff = time.time() - days * 86400
    removed = freed = 0
    try:
        names = os.listdir(root)
    except Exception:
        return (0, 0)
    for n in names:
        p = os.path.join(root, n)
        try:
            if not os.path.isfile(p):
                continue
            low = n.lower()
            if n == "gui_console.log" or low.startswith("secret_frame_"):
                continue
            if not (low.endswith(".log") or low.endswith(".png")):
                continue
            st = os.stat(p)
            if st.st_mtime >= cutoff:
                continue
            os.remove(p)
            removed += 1
            freed += st.st_size
        except Exception:
            continue
    if removed:
        try:
            append_file("gui_console.log",
                        f"🧹 启动清理：删掉 {removed} 个 {days} 天前的日志/截图，"
                        f"释放 {freed / 1048576:.1f} MB")
        except Exception:
            pass
    return (removed, freed)


def append_file(name: str, text: str) -> None:
    try:
        with open(os.path.join(diag_dir(), name), "a", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    except Exception:
        pass


def crash_log(title: str, text: str) -> None:
    append_file("gui_crash.log",
                f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {title} =====\n{text}")


def safe(fn):
    """槽函数兜底：异常只记录 + 写进界面日志，绝不让 PyQt5 abort 掉整个进程。

    坑（2026-11-17 16:53 用户实测 `TypeError: on_start() takes 1 positional argument
    but 2 were given`）：包装函数如果写成 `def wrapper(self, *a, **k)`，PyQt5 会认为它
    能接任意参数，于是把按钮 `clicked(checked=False)` 的布尔值也塞进来 → 原函数签名只接
    self，直接 TypeError。所以这里必须按原函数的**真实参数个数**生成定参包装，
    让 PyQt 的参数推断跟原函数一致。
    """
    n = 1
    code = getattr(fn, "__code__", None)
    if code is not None:
        n = code.co_argcount          # 含 self

    def _guard(self, *a):
        depth = getattr(self, "_safe_depth", 0)
        if depth > 3:                 # 防止「日志本身出错 → 递归报错」无限套娃
            return None
        self._safe_depth = depth + 1
        try:
            return fn(self, *a)
        except Exception:
            txt = traceback.format_exc()
            crash_log(f"界面操作异常：{fn.__name__}", txt)
            last = txt.strip().splitlines()[-1] if txt.strip() else "未知错误"
            try:
                if fn.__name__ != "write":
                    self.write("\n".join([
                        f"❌ 界面操作「{fn.__name__}」出错：{last}",
                        f"   详细记录：{os.path.join(diag_dir(), 'gui_crash.log')}",
                    ]))
            except Exception:
                pass
            return None
        finally:
            self._safe_depth = depth

    if n <= 1:
        @functools.wraps(fn)
        def wrapper(self):
            return _guard(self)
    elif n == 2:
        @functools.wraps(fn)
        def wrapper(self, a=None):
            return _guard(self, a)
    else:
        @functools.wraps(fn)
        def wrapper(self, a=None, b=None, c=None):
            return _guard(self, a, b, c)
    return wrapper


def need_files() -> list[str]:
    """core 目录里必须存在的文件。
    注意：engine.py 只在源码运行时需要 —— 打包后它被冻进 EXE，core 里没有，
    所以 frozen 时不能把它算进「缺失」（2026-11-17 16:47 实测踩到：会误报文件不完整）。"""
    need = ["full_loop.py", "game_auto.py", "configs/full_loop.json", "templates"]
    if not getattr(sys, "frozen", False):
        need.append("engine.py")
    return need


def boot_record() -> dict:
    """启动横幅：把环境写进 gui_boot.log，出问题一眼看出是哪台机器什么环境"""
    info = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ver": APP_VER,
        "frozen": bool(getattr(sys, "frozen", False)),
        "exe": sys.executable,
        "core": engine.core_dir(),
        "cwd": os.getcwd(),
        "python": sys.version.split()[0],
        "appdata": os.environ.get("APPDATA", ""),
        "diag_dir": diag_dir(),
    }
    info["missing"] = [k for k in need_files()
                       if not os.path.exists(os.path.join(engine.core_dir(), k))]
    inner = os.path.join(os.path.dirname(sys.executable), "_internal")
    # PyInstaller 6 的 onedir 才有 _internal/；Win7 版用 PyInstaller 5，依赖直接放 exe 同级
    # → 这里是 False 属于正常，不代表解压不完整（真正缺文件由上面的 missing 判定）。
    info["internal_dir"] = os.path.isdir(inner)
    info.update(env_report())        # dpi_mode / scale / phys_screen / monitors / admin / win
    try:
        from PyQt5.QtWidgets import QApplication
        scr = QApplication.primaryScreen()
        info["screen"] = f"{scr.size().width()}x{scr.size().height()}"
        info["dpi"] = round(float(scr.logicalDotsPerInch()), 1)
    except Exception:
        pass
    append_file("gui_boot.log", json.dumps(info, ensure_ascii=False))
    return info


# ---------------------------------------------------------------- 子进程分发

_FLAG_WITH_ARG = {"-W", "-X", "-c"}
_FLAG_ONLY = {"-u", "-B", "-E", "-s", "-S", "-O", "-OO", "-q", "-I", "-v", "-b"}


def _attach_fd(fd: int):
    """把某个 fd（1=stdout / 2=stderr）接回 Python —— 父进程用管道拉起我们时才有意义。

    背景（2026-11-17 17:05 实测）：打包成窗口程序后 sys.stdout 是 None，
    原来 fix_stdio() 直接把它换成 devnull，于是 gui → engine → 子脚本 这条链上
    game_auto.py 的 print 全被吞掉，界面上「① 开局」几分钟一行日志都不动，
    看着像卡死。但引擎其实是用 stdout=PIPE 拉起的，fd 1 还在，接回来就有输出。
    """
    try:
        os.fstat(fd)                     # 双击启动时 fd 无效 → 直接走 devnull
        os.write(fd, b"")                # 再探一次：句柄能写才敢接
        raw = io.FileIO(fd, "w", closefd=False)
        return io.TextIOWrapper(raw, encoding="utf-8", errors="replace",
                                line_buffering=True, write_through=True)
    except Exception:
        return None


def fix_stdio():
    """窗口程序没有控制台时 sys.stdout 可能是 None；优先接回管道，接不到才丢 devnull"""
    for name, fd in (("stdout", 1), ("stderr", 2)):
        s = getattr(sys, name, None)
        if s is None:
            setattr(sys, name, _attach_fd(fd) or open(os.devnull, "w", encoding="utf-8"))
        else:
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def dispatch_script(argv: list[str]) -> bool:
    """argv 里出现 .py 文件 → 用内置解释器跑它（返回 True 表示已接管）"""
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _FLAG_WITH_ARG:
            i += 2
            continue
        if tok in _FLAG_ONLY:
            i += 1
            continue
        if tok.startswith("-") and not tok.lower().endswith(".py"):
            i += 1
            continue
        if tok.lower().endswith(".py"):
            path = tok if os.path.isabs(tok) else os.path.join(os.getcwd(), tok)
            if not os.path.exists(path):
                path = os.path.join(engine.core_dir(), os.path.basename(tok))
            if not os.path.exists(path):
                print(f"[dispatch] 找不到脚本：{tok}", file=sys.stderr, flush=True)
                raise SystemExit(2)
            os.chdir(os.path.dirname(path) or ".")
            sys.path.insert(0, os.path.dirname(path))
            sys.argv = [path] + argv[i + 1:]
            fix_stdio()
            # 子脚本抛异常时，PyInstaller 会弹一个「Unhandled exception in script」的
            # Python 原始堆栈框，对用户毫无意义（2026-11-17 18:38 用户截图就是这个）。
            # 这里拦下来：堆栈照旧写日志，屏幕上只留一句人话 + 退出码。
            try:
                runpy.run_path(path, run_name="__main__")
            except SystemExit:
                raise
            except BaseException as e:
                import traceback
                try:
                    print(traceback.format_exc(), file=sys.stderr, flush=True)
                except Exception:
                    pass
                msg = str(e) or e.__class__.__name__
                print(f"❌ {os.path.basename(path)} 出错：{msg}", flush=True)
                print("   （详细堆栈见 core/logs/gui_console.log）", flush=True)
                raise SystemExit(3)
            return True
        return False
    return False


# ---------------------------------------------------------------- 界面
def gui_main() -> int:
    # 启动顺手清理 3 天前的一次性日志（2026-11-19 21:05 用户定；失败静默，绝不挡启动）
    try:
        cleanup_old_logs()
    except Exception:
        pass

    # 注意：本机是 Windows 10 1607（build 14393），Qt6（PySide6）会在加载时报
    # 「找不到指定的程序」——Qt 6.5+ 要求 1809+。所以图形界面用 Qt5（PyQt5）。
    from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
    from PyQt5.QtGui import QFont, QTextCursor
    from PyQt5.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QScrollArea,
        QGridLayout, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
        QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
    )

    # 参数存到「用户目录」，不放在 EXE 目录里 —— 这样重新打包/换新版本都不会把
    # 你填好的数字冲掉（旧版放在 core/configs 里，一打包就被清空，看起来就像「老是默认值」）
    def _settings_path() -> str:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "KKAuto")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return os.path.join(engine.core_dir(), "configs", "gui_settings.json")
        return os.path.join(d, "gui_settings.json")

    CFG = _settings_path()
    LEGACY_CFG = os.path.join(engine.core_dir(), "configs", "gui_settings.json")

    # 默认参数（用户 2026-11-18 00:06）：30 局 / 钓鱼 2 次 / 每 10 局签到领券 / 无限轮
    DEFAULTS = dict(n_games=30, n_fishing=2, fishing_hours=5.0,
                    claim_every=10, loops=0, stop_on_fail=False,
                    busy_wait=6,         # 「您正在游戏中，不能创建游戏房间」每次间隔多少分钟
                    busy_attempts=2,     # 「搜图→建房」整段最多试几次（用户 2026-11-20 17:02 定 2 次）
                    busy_restart=True,   # 试满还不放行 → 自动重启平台
                    order="full_first",  # 每轮先后：full_first 先通关 / fish_first 先钓鱼
                    max_round_min=50,   # 单局超过 N 分钟没打完 → 杀 War3 重开游戏、重跑本轮（不重启平台）
                    settle_min=6,       # 等清算（胜利/失败）最多等几分钟；超时 → 自动退出游戏（2026-11-21 19:44 用户定，默认 6 分钟）
                    debug=False)        # 调试开关（2026-11-25）：记找图分数/点击坐标 + 存带标记截图

    def load_cfg() -> dict:
        for p in (CFG, LEGACY_CFG):
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            out = dict(DEFAULTS)
            out.update({k: d[k] for k in DEFAULTS if k in d})
            if p != CFG:
                save_cfg(out)       # 旧位置的值 → 平滑迁移到用户目录
            return out
        return dict(DEFAULTS)

    def save_cfg(d: dict):
        try:
            os.makedirs(os.path.dirname(CFG), exist_ok=True)
            with open(CFG, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    class LoopThread(QThread):
        line = pyqtSignal(str)
        state = pyqtSignal(str)
        rc = pyqtSignal(int)

        def __init__(self, params):
            super().__init__()
            self.params = params
            self.loop = None

        def run(self):
            # debug 只是界面开关（已经写进环境变量给子进程），engine.Loop 不认这个参数
            kw = dict(self.params)
            kw.pop("debug", None)
            self.loop = engine.Loop(on_line=self.line.emit,
                                    on_state=self.state.emit, **kw)
            self.rc.emit(self.loop.run())

        def stop(self):
            if self.loop:
                self.loop.stop()

    class Win(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"{APP_TITLE}  {APP_VER}")
            self.resize(980, 720)
            self.thread = None
            self.t0 = 0
            self.running = False
            # 2026-11-21 18:54 用户要求：运行统计保留上次记录（点「开始运行」才清零）
            self.stats = self._load_stats()
            cfg = load_cfg()

            root = QWidget()
            self.setCentralWidget(root)
            lay = QVBoxLayout(root)
            lay.setContentsMargins(18, 14, 18, 14)
            lay.setSpacing(10)

            # 标题
            head = QHBoxLayout()
            title = QLabel(f"我在峡谷打僵尸  {APP_VER}")   # 2026-11-19 21:16：自绘标题带版本号，验收一眼可见
            title.setObjectName("h1")
            head.addWidget(title)
            sub = QLabel("通关循环 × 钓鱼模式 × 签到领券")
            sub.setObjectName("sub")
            head.addWidget(sub)
            head.addStretch(1)
            self.badge = QLabel("待机")
            self.badge.setObjectName("badge")
            head.addWidget(self.badge)
            lay.addLayout(head)

            # 参数卡片
            card = QFrame()
            card.setObjectName("card")
            g = QGridLayout(card)
            g.setContentsMargins(16, 14, 16, 14)
            g.setHorizontalSpacing(14)
            g.setVerticalSpacing(10)

            def lab(t):
                w = QLabel(t)
                w.setObjectName("lab")
                return w

            self.sp_games = QSpinBox()
            self.sp_games.setRange(1, 999999)
            self.sp_games.setValue(int(cfg["n_games"]))
            self.sp_games.setSuffix(" 局")

            self.sp_fish = QSpinBox()
            self.sp_fish.setRange(0, 99999)
            self.sp_fish.setValue(int(cfg["n_fishing"]))
            self.sp_fish.setSuffix(" 次")

            self.sp_hours = QDoubleSpinBox()
            self.sp_hours.setRange(0.1, 24.0)
            self.sp_hours.setDecimals(1)
            self.sp_hours.setSingleStep(0.5)
            self.sp_hours.setValue(float(cfg["fishing_hours"]))
            self.sp_hours.setSuffix(" 小时")

            self.sp_claim = QSpinBox()
            self.sp_claim.setRange(0, 999999)
            self.sp_claim.setValue(int(cfg["claim_every"]))
            self.sp_claim.setSuffix(" 局")
            self.sp_claim.setSpecialValueText("关闭")

            self.sp_loops = QSpinBox()
            self.sp_loops.setRange(0, 999999)
            self.sp_loops.setValue(int(cfg["loops"]))
            self.sp_loops.setSuffix(" 轮")
            self.sp_loops.setSpecialValueText("无限")

            # 每轮先跑哪一段（用户 2026-11-17 23:44：想自己选优先通关还是优先钓鱼）
            self.cb_order = QComboBox()
            self.cb_order.addItem("先通关循环 → 后钓鱼模式", "full_first")
            self.cb_order.addItem("先钓鱼模式 → 后通关循环", "fish_first")
            self.cb_order.setCurrentIndex(
                1 if str(cfg.get("order", "full_first")) == "fish_first" else 0)
            self.cb_order.setToolTip("只决定每轮两段的先后，局数/次数照旧")

            self.sp_busy = QSpinBox()
            self.sp_busy.setRange(0, 1440)
            self.sp_busy.setValue(int(cfg.get("busy_wait", 6)))
            self.sp_busy.setSuffix(" 分钟")
            self.sp_busy.setSpecialValueText("不等")

            # 「无法创建房间」时整段（搜图 → 建房）试几次（用户 2026-11-17 15:07：4 次）
            self.sp_attempts = QSpinBox()
            self.sp_attempts.setRange(1, 999)
            self.sp_attempts.setValue(int(cfg.get("busy_attempts", 2)))
            self.sp_attempts.setSuffix(" 次")

            # 试满 N 次还不放行 → 自动重启平台（用户 2026-11-17 15:07）
            self.cb_busy_restart = QCheckBox("试满都不行就自动重启平台（关掉平台重开 → 等大厅 → 再建房）")
            self.cb_busy_restart.setChecked(bool(cfg.get("busy_restart", True)))

            # 单局看门狗（用户 2026-11-17 15:20）：一局超过 N 分钟还没打完 → 杀 War3 重开游戏、重跑本轮
            # （这条不再触发平台重启，平台重启只留给「无法创建房间」）
            self.sp_stuck = QSpinBox()
            self.sp_stuck.setRange(0, 1440)
            self.sp_stuck.setSingleStep(5)
            self.sp_stuck.setValue(int(cfg.get("max_round_min", 50)))
            self.sp_stuck.setSuffix(" 分钟")
            self.sp_stuck.setSpecialValueText("不限")

            # 等清算（2026-11-20 16:33 用户：秘境/收尾等清算，超时自动退出；成品软件里可改）
            self.sp_settle = QSpinBox()
            self.sp_settle.setRange(1, 180)
            self.sp_settle.setSingleStep(1)
            self.sp_settle.setValue(int(cfg.get("settle_min", 6)))
            self.sp_settle.setSuffix(" 分钟")
            self.sp_settle.setToolTip("秘境挑战/收尾等待结束（胜利或失败）的最长时间；到点还没结束就自动退出游戏")


            self.cb_fail = QCheckBox("任一段失败就停（默认继续跑）")
            self.cb_fail.setChecked(bool(cfg["stop_on_fail"]))

            g.addWidget(lab("每轮通关局数"), 0, 0)
            g.addWidget(self.sp_games, 0, 1)
            g.addWidget(lab("每轮钓鱼次数"), 0, 2)
            g.addWidget(self.sp_fish, 0, 3)
            g.addWidget(lab("每次钓鱼时长"), 1, 0)
            g.addWidget(self.sp_hours, 1, 1)
            g.addWidget(lab("每累计几局签到领券"), 1, 2)
            g.addWidget(self.sp_claim, 1, 3)
            g.addWidget(lab("大循环轮数"), 2, 0)
            g.addWidget(self.sp_loops, 2, 1)
            g.addWidget(lab("优先顺序"), 2, 2)
            g.addWidget(self.cb_order, 2, 3)
            g.addWidget(lab("卡「正在游戏中」时等待"), 3, 0)
            g.addWidget(self.sp_busy, 3, 1)
            g.addWidget(lab("最多尝试"), 3, 2)
            g.addWidget(self.sp_attempts, 3, 3)
            # 2026-11-20 17:01 用户要求：「试满重启」紧跟「最多尝试」——它们是一个处理链条
            g.addWidget(self.cb_busy_restart, 4, 0, 1, 4)
            g.addWidget(self.cb_fail, 5, 0, 1, 4)
            g.addWidget(lab("单局超过多久没打完就重开游戏"), 6, 0)
            g.addWidget(self.sp_stuck, 6, 1)
            g.addWidget(lab("秘境挑战等待结束"), 7, 0)
            g.addWidget(self.sp_settle, 7, 1)
            g.setColumnStretch(1, 1)
            g.setColumnStretch(3, 1)
            lay.addWidget(card)

            # 计划预览：默认折叠（用户 2026-11-17 16:23）。
            # 无限轮 × 每轮上百局时，这一行会刷成一大片文字把参数区和合计行挤走，
            # 所以改成「一行小按钮 + 需要时点开」，合计行照旧常显。
            pbar = QHBoxLayout()
            pbar.setContentsMargins(0, 0, 0, 0)
            pbar.setSpacing(8)
            self.btn_plan = QPushButton("▸ 计划预览（点开看每轮怎么拆）")
            self.btn_plan.setObjectName("mini")
            self.btn_plan.setCheckable(True)
            self.btn_plan.setCursor(Qt.PointingHandCursor)
            pbar.addWidget(self.btn_plan)
            pbar.addStretch(1)
            lay.addLayout(pbar)

            self.plan = QLabel()
            self.plan.setObjectName("plan")
            self.plan.setWordWrap(True)
            self.plan.setVisible(False)
            lay.addWidget(self.plan)
            # 连接必须放在 self.plan 建好之后，否则 setChecked 触发槽函数会撞 AttributeError
            self.btn_plan.toggled.connect(self.toggle_plan)
            self.btn_plan.setChecked(False)   # 默认收起
            # 合计行：让你一眼看到「这次到底会打多少局」（用户 10:38：参数要完全听我的）
            self.plan_sum = QLabel()
            self.plan_sum.setObjectName("psum")
            self.plan_sum.setWordWrap(True)
            lay.addWidget(self.plan_sum)

            # 2026-11-21 18:00 用户定：运行时显示「运行统计」面板（参考图样式：5 个大数字 + 分轮明细）
            self.stat_panel = QFrame()
            self.stat_panel.setObjectName("statpanel")
            self.stat_panel.setVisible(False)
            _sl = QVBoxLayout(self.stat_panel)
            _sl.setContentsMargins(12, 10, 12, 10)
            _sl.setSpacing(6)
            _hd = QLabel("运行统计")
            _hd.setObjectName("stathead")
            _sl.addWidget(_hd)
            _top = QHBoxLayout()
            _top.setSpacing(18)
            self.stat_vals = {}
            for _k, _lab in (("loop", "当前轮次"), ("games", "已打局数"), ("cleared", "通关"),
                             ("fishing", "钓鱼"), ("claims", "签到"), ("dur", "本轮耗时")):
                _box = QVBoxLayout()
                _box.setSpacing(1)
                _l1 = QLabel(_lab)
                _l1.setObjectName("statlbl")
                _v = QLabel("0")
                _v.setObjectName(f"statval_{_k}")
                _box.addWidget(_l1)
                _box.addWidget(_v)
                _top.addLayout(_box)
                self.stat_vals[_k] = _v
            _top.addStretch(1)
            _sl.addLayout(_top)
            # 2026-11-21 18:10 用户要求：分轮明细区固定「6 轮」高度，超出用滚轮/上下键滑动
            self.stat_scroll = QScrollArea()
            self.stat_scroll.setObjectName("statscroll")
            self.stat_scroll.setWidgetResizable(True)
            self.stat_scroll.setFixedHeight(3 * 26 + 8)   # 2026-11-21 18:15 用户定：固定显示 3 轮
            self.stat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.stat_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            _inner = QWidget()
            self.stat_rows = QVBoxLayout(_inner)
            self.stat_rows.setContentsMargins(0, 0, 4, 0)
            self.stat_rows.setSpacing(3)
            self.stat_rows.addStretch(1)
            self.stat_scroll.setWidget(_inner)
            _sl.addWidget(self.stat_scroll)
            self.stat_foot = QLabel("")
            self.stat_foot.setObjectName("statfoot")
            _sl.addWidget(self.stat_foot)
            lay.addWidget(self.stat_panel)

            self.cb_order.currentIndexChanged.connect(self.refresh_plan)
            self.cb_order.currentIndexChanged.connect(self.autosave_cfg)
            for w in (self.sp_games, self.sp_fish, self.sp_hours, self.sp_claim, self.sp_loops):
                w.valueChanged.connect(self.refresh_plan)
                w.valueChanged.connect(self.autosave_cfg)   # 改完立即落盘，重启还在
            self.sp_busy.valueChanged.connect(self.autosave_cfg)
            self.sp_attempts.valueChanged.connect(self.autosave_cfg)
            self.sp_stuck.valueChanged.connect(self.autosave_cfg)
            self.cb_busy_restart.stateChanged.connect(self.autosave_cfg)
            self.cb_fail.stateChanged.connect(self.autosave_cfg)
            self.refresh_plan()

            # 按钮
            row = QHBoxLayout()
            self.btn_start = QPushButton("开始运行")
            self.btn_start.setObjectName("start")
            self.btn_start.clicked.connect(self.on_start)
            self.btn_stop = QPushButton("停止")
            self.btn_stop.setObjectName("stop")
            self.btn_stop.setEnabled(False)
            self.btn_stop.clicked.connect(self.on_stop)
            # 2026-11-21 18:36 用户要求：运行统计按钮放在「停止」和「只看计划」之间
            self.btn_stats = QPushButton("▸ 运行统计")
            self.btn_stats.setCheckable(True)
            self.btn_stats.setCursor(Qt.PointingHandCursor)
            self.btn_stats.toggled.connect(
                lambda c: (self.stat_panel.setVisible(c),
                           self.btn_stats.setText("▾ 收起运行统计" if c else "▸ 运行统计")))
            self.btn_dry = QPushButton("只看计划")
            self.btn_dry.setCheckable(True)     # 2026-11-21 18:32 用户要求：点一下显示计划、再点收起
            self.btn_dry.setCursor(Qt.PointingHandCursor)
            self.btn_dry.toggled.connect(self.on_dry)
            self.lbl_time = QLabel("")
            self.lbl_time.setObjectName("timer")
            row.addWidget(self.btn_start, 0)
            row.addWidget(self.btn_stop, 0)
            row.addWidget(self.btn_stats, 0)
            row.addWidget(self.btn_dry, 0)
            row.addStretch(1)
            row.addWidget(self.lbl_time)
            lay.addLayout(row)

            self.state = QLabel("当前阶段：待机")
            self.state.setObjectName("state")
            lay.addWidget(self.state)

            # 日志
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setObjectName("log")
            # 0 = 不限制（Qt 的自动裁剪是从开头裁，倒序显示时开头是最新行，会误删）；
            # 改由 write() 自己裁末尾最旧的行，见 LOG_MAX_LINES。
            self.log.setMaximumBlockCount(0)
            f = QFont("Consolas")
            f.setStyleHint(QFont.Monospace)
            f.setPointSize(9)
            self.log.setFont(f)
            lay.addWidget(self.log, 1)

            foot = QHBoxLayout()
            hint = QLabel("⚠ 运行期间会自动操作鼠标键盘，请勿干预（鼠标别挪到屏幕边缘，会触发紧急停止）")
            hint.setObjectName("hint")
            foot.addWidget(hint)
            foot.addStretch(1)
            # 一键打包日志（用户 2026-11-18 16:40：成品要一个按钮，点一下就能拿到日志发我）
            self.btn_pack = QPushButton("打包日志发我")
            self.btn_pack.setObjectName("packlog")
            self.btn_pack.setToolTip("把全部日志 + 环境信息收进桌面一个 zip，直接拖进聊天窗口发出去")
            self.btn_pack.setCursor(Qt.PointingHandCursor)
            self.btn_pack.clicked.connect(self.on_pack_logs)
            b1 = QPushButton("打开 logs")
            b1.clicked.connect(lambda: self.open_dir(engine.logs_dir()))
            b2 = QPushButton("打开 templates")
            b2.clicked.connect(lambda: self.open_dir(os.path.join(engine.core_dir(), "templates")))
            b3 = QPushButton("清空日志")
            b3.clicked.connect(self.log.clear)
            # 调试开关 + 抓现场（2026-11-25 用户：去 Win7 那台机器测，怕坐标/图色异常）
            self.btn_dbg = QPushButton()
            self.btn_dbg.setObjectName("dbg")
            self.btn_dbg.setCheckable(True)
            self.btn_dbg.setCursor(Qt.PointingHandCursor)
            self.btn_dbg.setToolTip(
                "勾上后运行期间会记录：每次找图的最高分与阈值、每次点击的客户区坐标/屏幕坐标/缩放系数，"
                "并在点击前存一张带十字标记的截图。\n"
                "产物在 core\\logs\\debug\\<时间>\\，点「打包日志发我」会一起打包。\n"
                "⚠ 会略微变慢（每次点击多一次截图），排查完记得关掉。")
            self._debug_on = bool(cfg.get("debug"))
            self.btn_dbg.setChecked(self._debug_on)
            self.btn_dbg.toggled.connect(self.on_toggle_debug)

            self.btn_snap = QPushButton("抓现场")
            self.btn_snap.setCursor(Qt.PointingHandCursor)
            self.btn_snap.setToolTip(
                "立刻存一份现场：全屏截图 + 每个窗口的客户区截图 + 屏幕/DPI/缩放信息 + "
                "全部模板在当前画面上的实际匹配分数。\n"
                "排查「坐标不对 / 图色认不出」时先点这个，再点「打包日志发我」。")
            self.btn_snap.clicked.connect(self.on_snapshot)

            # 2026-11-26 23:05 用户：成品要一个「截图」按钮 —— 按下先把本软件藏掉，
            # 再截一张整屏（不带自己的界面），存 core\logs\shot\。
            self.btn_shot = QPushButton("截图")
            self.btn_shot.setCursor(Qt.PointingHandCursor)
            self.btn_shot.setToolTip(
                "按下后：先把本软件窗口隐藏 → 截一张整屏 PNG → 自动恢复窗口。\n"
                "存到 core\\logs\\shot\\，路径会复制到剪贴板，点「打包日志发我」也会一起带走。\n"
                "和「抓现场」的区别：这个只截屏、几百毫秒完成，不带窗口清单和模板打分。")
            self.btn_shot.clicked.connect(self.on_screenshot)

            foot.addWidget(self.btn_dbg)
            foot.addWidget(self.btn_shot)
            foot.addWidget(self.btn_snap)
            foot.addWidget(self.btn_pack)
            self._debug_ui()
            self._debug_env(self._debug_on)
            foot.addWidget(b1)
            foot.addWidget(b2)
            foot.addWidget(b3)
            lay.addLayout(foot)

            self.timer = QTimer(self)
            self.timer.setInterval(1000)
            self.timer.timeout.connect(self.tick)

            # 空闲监视已删除（2026-11-19 23:18 用户定「待机状态这个给我关闭」）：
            # 没点「开始运行」时不跑任何定时检查，纯待机只剩界面本身（~0% CPU）。
            # 登录窗拦截由点开始时的「启动前检查」+ 任务期间的守卫线程负责。

            # 启动信息作为一整块插入（倒序显示时块内顺序保持原样，读起来才是从上往下）
            self.write("\n".join([
                f"工作目录：{engine.core_dir()}",
                f"参数文件：{CFG}（改完自动保存，换新版本也不会丢）",
                f"Python 运行环境：{'打包 EXE' if engine.FROZEN else sys.executable}",
                "参数全部由你定：改完立刻记住（下次打开还是这些数）。",
                "黄色「合计」行就是这次实际要打的总局数 —— 跟它不一致就是没按你的参数跑，直接反馈。",
            ]))

        # ---------- 工具 ----------
        @safe
        def write(self, s: str):
            """倒序写入：最新一块永远在最上面（用户 2026-11-17 20:05 要求）

            具体插入/裁剪逻辑在模块级 insert_block_at_top()，方便离线自检。
            多行消息请一次传进来（"a\\nb"），它作为一块整体插到顶部，块内顺序不变。
            """
            logw = getattr(self, "log", None)
            if logw is None:
                # __init__ 里界面还没建好时（refresh_plan 的早期回调）只落文件，别炸
                print(s, flush=True)
                return
            insert_block_at_top(logw, s)
            self._parse_stats(s)
            # 镜像到文件：万一窗口被强行关掉/进程被杀，日志还在（文件里保持正常时序）
            # （子脚本的行自带 [HH:MM:SS]，别再叠一层时间戳）
            mark = "" if s.lstrip().startswith("[") else f"[{time.strftime('%H:%M:%S')}] "
            append_file("gui_console.log", mark + s)

        def _stats_file(self):
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
            return os.path.join(base, "KKAuto", "run_stats.json")

        def _load_stats(self):
            """启动时读上次的运行统计（保留上次记录）"""
            try:
                p = self._stats_file()
                if os.path.exists(p):
                    d = json.load(io.open(p, encoding="utf-8"))
                    return dict(games=int(d.get("games", 0)), cleared=int(d.get("cleared", 0)),
                                fishing=int(d.get("fishing", 0)), claims=int(d.get("claims", 0)),
                                loop=int(d.get("loop", 0)),
                                by_loop=d.get("by_loop", {}) or {})
            except Exception:
                pass
            return dict(games=0, cleared=0, fishing=0, claims=0, loop=0, by_loop={})

        def _save_stats(self):
            """每次统计变化就落盘（程序关掉也不丢）"""
            try:
                p = self._stats_file()
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with io.open(p, "w", encoding="utf-8") as f:
                    json.dump(self.stats, f, ensure_ascii=False)
            except Exception:
                pass

        def _parse_stats(self, s: str):
            """从日志行累加运行统计（局数/通关/钓鱼/签到/轮次 + 分轮明细），实时刷新统计面板"""
            if not getattr(self, "running", False):
                return
            changed = False
            st = self.stats

            def _bl():
                lp = st.get("loop") or 1
                return st["by_loop"].setdefault(lp, dict(games=0, cleared=0, fishing=0, claims=0))

            try:
                import re as _re
                m = _re.search(r"# 第 (\d+)/", s)
                if m:
                    _new = int(m.group(1))
                    _now = time.time()
                    # 2026-11-21 18:40 用户要求：记录每轮起止，算「完成一轮时间」
                    if st["loop"] and st["loop"] in st["by_loop"]:
                        st["by_loop"][st["loop"]]["t1"] = _now
                    st["loop"] = _new
                    b0 = st["by_loop"].setdefault(
                        _new, dict(games=0, cleared=0, fishing=0, claims=0))
                    b0.setdefault("t0", _now)
                    b0.pop("t1", None)
                    changed = True
                if "第 " in s and " 轮：" in s and "A 进场=" in s:
                    st["games"] += 1
                    b = _bl()
                    b["games"] += 1
                    if "A 进场=成功" in s:
                        st["cleared"] += 1
                        b["cleared"] += 1
                    changed = True
                if "签到领券】开始" in s:
                    st["claims"] += 1
                    _bl()["claims"] += 1
                    st["task"] = "签到"
                    changed = True
                if "已确认进入钓鱼场景" in s or "钓鱼模式 第" in s:
                    if "已确认进入钓鱼场景" in s:
                        st["fishing"] += 1
                        _bl()["fishing"] += 1
                    st["task"] = "钓鱼"
                    changed = True
                if "通关循环 ×" in s:
                    st["task"] = "通关"
                    changed = True
            except Exception:
                pass
            if changed:
                try:
                    self._refresh_stats()
                except Exception:
                    pass

        def _refresh_stats(self):
            st = self.stats
            if not getattr(self, "running", False):
                return
            try:
                self.stat_vals["loop"].setText(f"第 {st['loop'] or 1} 轮")
                self.stat_vals["games"].setText(str(st["games"]))
                self.stat_vals["cleared"].setText(str(st["cleared"]))
                self.stat_vals["fishing"].setText(f"{st['fishing']} 次")
                self.stat_vals["claims"].setText(f"{st['claims']} 次")
                _b = st["by_loop"].get(st["loop"] or 1) or {}
                if _b.get("t0"):
                    _s = int(time.time() - _b["t0"])
                    self.stat_vals["dur"].setText(
                        f"{_s // 60}分{_s % 60:02d}秒" if _s >= 60 else f"{_s}秒")
                else:
                    self.stat_vals["dur"].setText("--")
                # 分轮明细（清空重建）
                while self.stat_rows.count():
                    it = self.stat_rows.takeAt(0)
                    w = it.widget() if it else None
                    if w:
                        w.deleteLater()
                loops = sorted(st["by_loop"].keys())
                _now = time.time()
                for lp in loops:
                    b = st["by_loop"][lp]
                    cur = (lp == (st["loop"] or 1))
                    _t0 = b.get("t0")
                    _t1 = b.get("t1") or (_now if cur else None)
                    dur = ""
                    if _t0 and _t1 and _t1 >= _t0:
                        _s = int(_t1 - _t0)
                        dur = f"   {_s // 60}分{_s % 60:02d}秒" if _s >= 60 else f"   {_s}秒"
                    if cur:
                        # 2026-11-22 17:47 用户要求：进行中的轮合并成一行（局数=当前局，状态「通关任务中」）
                        _t0 = b.get("t0")
                        _s = int((_now - _t0)) if _t0 else 0
                        _d = f"   {_s // 60}分{_s % 60:02d}秒" if _s >= 60 else f"   {_s}秒" if _s else ""
                        _task = st.get("task") or "通关"
                        r1 = QLabel(f"第 {lp} 轮     {b['games'] + 1} 局通关   钓鱼 {b['fishing']}   签到 {b['claims']}{_d}   {_task}中")
                        r1.setWordWrap(True)
                        r1.setObjectName("statrow_cur")
                        self.stat_rows.addWidget(r1)
                        continue
                    else:
                        # 2026-11-21 18:47 用户要求：完成轮显示「完成的具体日期时间」
                        try:
                            # 2026-11-21 18:49 用户要求：去掉年份和秒 → 只留「11月21日 19:20」
                            _ft = time.strftime("%m月%d日 %H:%M", time.localtime(b.get("t1") or _now))
                        except Exception:
                            _ft = ""
                        tail = f"   {_ft} 点完成" if _ft else "   完成"
                    # 2026-11-22 17:20 用户要求：只有第 1 轮时，这一行显示为「大循环」
                    if len(loops) == 1 and (st["loop"] or 1) == 1:
                        _label = "大循环  "
                    else:
                        _label = f"第 {lp} 轮"
                    txt = (f"{_label}     {b['games']} 局通关   钓鱼 {b['fishing']}   签到 {b['claims']}{dur}{tail}")
                    row = QLabel(txt)
                    row.setWordWrap(True)
                    row.setObjectName("statrow_cur" if cur else "statrow")
                    self.stat_rows.addWidget(row)
                self.stat_foot.setText(
                    f"参数：每轮 {self.sp_games.value()} 局 · 钓鱼 {self.sp_fish.value()} 次 · "
                    f"每 {self.sp_claim.value()} 局签到")
                self._save_stats()
            except Exception:
                pass

        def set_badge(self, text, on=False):
            self.badge.setText(text)
            self.badge.setProperty("on", bool(on))
            self.badge.style().unpolish(self.badge)
            self.badge.style().polish(self.badge)

        def open_dir(self, p):
            try:
                os.startfile(p)  # noqa: S606
            except Exception as e:
                self.write(f"打开目录失败：{e!r}")
        def on_pack_logs(self):
            """一键打包日志（用户 2026-11-18 16:40：成品要一个按钮，点一下就能拿到日志发我）

            实测收 121 个文件 0.3 秒，直接在 UI 线程里跑，不折腾后台线程；
            打完压成桌面上的一个 zip，并在资源管理器里选中它 —— 拖进聊天窗口就能发。
            """
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                if engine.core_dir() not in sys.path:
                    sys.path.insert(0, engine.core_dir())
                import logpack
                info = logpack.pack(extra_text=self.log.toPlainText())
            except Exception:
                QApplication.restoreOverrideCursor()
                tb = traceback.format_exc(limit=4)
                self.write("打包日志失败：\n" + tb)
                QMessageBox.warning(self, "打包日志失败", tb)
                return
            QApplication.restoreOverrideCursor()

            zip_path = info.get("zip") or ""
            target = zip_path or info["out"]
            self.write(f"日志已打包：{info['copied']}/{info['total']} 个文件 → {target}")
            msg = f"已收集 {info['copied']} 个文件（共发现 {info['total']} 个）\n\n"
            msg += (f"压缩包：\n{zip_path}\n\n" if zip_path else f"文件夹：\n{info['out']}\n\n")
            msg += "把这个 zip 直接拖进聊天窗口发给我就行。"
            if info.get("failed"):
                msg += f"\n\n（{len(info['failed'])} 个文件正被占用没拷到，不影响）"
            QMessageBox.information(self, "日志已打包", msg)
            try:
                subprocess.Popen(f'explorer /select,"{target}"', creationflags=0x08000000)
            except Exception:
                self.open_dir(info["out"])

        # ------------------------------------------------ 调试开关 / 抓现场
        # 2026-11-25 用户：「成品新增调试按钮开关，晚点要去 W7 系统测试，可能会出现
        # 坐标/图色异常导致无法正常操作」。没有现场就只能猜，所以这里把现场留下来。
        def _debug_env(self, on: bool):
            """调试状态同步到环境变量 —— 子进程（full_loop 等）靠继承拿到同一个开关"""
            if on:
                os.environ["KK_DEBUG"] = "1"
            else:
                os.environ.pop("KK_DEBUG", None)
                os.environ.pop("KK_DEBUG_DIR", None)

        def _debug_ui(self):
            """按钮文字跟着开关走（不用问号图标，一眼知道现在是开还是关）"""
            on = bool(self.btn_dbg.isChecked())
            self.btn_dbg.setText("调试：开" if on else "调试：关")

        @safe
        def on_toggle_debug(self, checked):
            self._debug_on = bool(checked)
            self._debug_env(self._debug_on)
            self._debug_ui()
            self.autosave_cfg()
            if not self._debug_on:
                self.write("🐞 调试已关闭（已记录的现场不会删，仍在 core\\logs\\debug）")
                return
            path = ""
            try:
                if engine.core_dir() not in sys.path:
                    sys.path.insert(0, engine.core_dir())
                import dbgprobe
                path = dbgprobe.session_dir()
            except Exception:
                path = ""
            self.write("🐞 调试已开启：后续每次找图会记「最高分/阈值」，每次点击会记"
                       "「客户区坐标 + 屏幕坐标 + 缩放系数」，并在点击前存一张带十字的截图")
            self.write(f"   现场目录：{path}" if path else "   现场目录：core\\logs\\debug")
            self.write("   排查完记得关掉（每次点击多一次截图，会稍慢）；"
                       "出问题点「打包日志发我」，debug 目录会一起打包")

        @safe
        def on_screenshot(self):
            """截图：先把本软件窗口藏起来，再截整屏（用户 2026-11-26 23:05 要求）。

            和「抓现场」的区别：这里只做一件事 —— 把本软件藏掉、截一张干净的整屏，
            几百毫秒完成，存 core\\logs\\shot\\。用来补「日志里缺现场图」的证据。
            """
            if getattr(self, "_shot_busy", False):
                self.write("… 上一张截图还没跑完，稍等一下")
                return
            self._shot_busy = True
            try:
                if engine.core_dir() not in sys.path:
                    sys.path.insert(0, engine.core_dir())
                import dbgprobe
                self.write("📷 截图：先隐藏本软件窗口，再截整屏…")
                QApplication.processEvents()
                self.hide()
                QApplication.processEvents()
                time.sleep(0.5)          # 等桌面把被本软件挡住的窗口重新画出来
                p = dbgprobe.grab_desktop(tag="截图")
                if p:
                    try:
                        QApplication.clipboard().setText(p)
                    except Exception:
                        pass
                    self.write(f"✅ 已截图（本软件已隐藏）：{p}")
                    self.write("   路径已复制到剪贴板；点「打包日志发我」会一起带走")
                else:
                    self.write("⚠ 截图失败（grab_desktop 返回空：桌面被锁 / 显示器切换 / 抓图 API 异常）")
            except Exception:
                self.write("截图失败：\n" + traceback.format_exc(limit=4))
            finally:
                self.show()
                self.raise_()
                self.activateWindow()
                self._shot_busy = False

        def on_snapshot(self):
            """一键抓现场：截图 + 窗口 + 环境 + 全部模板打分（同步跑，约 3~15 秒）"""
            if getattr(self, "_snap_busy", False):
                self.write("… 上一次「抓现场」还没跑完，稍等一下")
                return
            self._snap_busy = True
            QApplication.setOverrideCursor(Qt.WaitCursor)
            self.write("▶ 抓现场：截屏 + 枚举窗口 + 给全部模板打分（约 3~15 秒，别关窗口）…")
            QApplication.processEvents()
            try:
                if engine.core_dir() not in sys.path:
                    sys.path.insert(0, engine.core_dir())
                import dbgprobe
                res = dbgprobe.snapshot(tag="手动")
                for line in res.get("summary") or []:
                    self.write("   " + str(line))
                rows = res.get("templates") or []
                if rows:
                    top = "，".join(f"{r['tpl']}={r['score_half']:.3f}" for r in rows[:6])
                    self.write(f"   分数最高的模板：{top}")
                    low = [r for r in rows if r["score_half"] >= 0.60]
                    if not low:
                        self.write("   ⚠ 没有任何模板超过 0.60 —— 画面可能不是预期界面/窗口绑错了")
                self.write(f"✅ 现场已存：{res.get('dir')}")
                self.write("   接着点「打包日志发我」，把 zip 发出来就能对照排查")
            except Exception:
                self.write("抓现场失败：\n" + traceback.format_exc(limit=4))
            finally:
                QApplication.restoreOverrideCursor()
                self._snap_busy = False

        def params(self) -> dict:
            return dict(n_games=self.sp_games.value(),
                        n_fishing=self.sp_fish.value(),
                        fishing_hours=self.sp_hours.value(),
                        claim_every=self.sp_claim.value(),
                        loops=self.sp_loops.value(),
                        stop_on_fail=self.cb_fail.isChecked(),
                        debug=bool(getattr(self, "btn_dbg", None)
                                   and self.btn_dbg.isChecked()),
                        busy_wait=self.sp_busy.value(),
                        busy_attempts=self.sp_attempts.value(),
                        busy_restart=self.cb_busy_restart.isChecked(),
                        order=self.cb_order.currentData() or "full_first",
                        max_round_min=self.sp_stuck.value(),
                        settle_min=self.sp_settle.value())

        @safe
        def autosave_cfg(self, *_):
            """参数一改就存盘：下次打开 EXE 还是你填的数字，不会被默认值顶掉"""
            save_cfg(self.params())

        @safe
        def toggle_plan(self, checked=None):
            """展开/收起计划预览（默认收起，避免无限轮时刷一大片文字挡住参数区）"""
            show = bool(self.btn_plan.isChecked() if checked is None else checked)
            self.plan.setVisible(show)
            self.btn_plan.setText("▾ 收起计划预览" if show
                                  else "▸ 计划预览（点开看每轮怎么拆）")

        @safe
        def refresh_plan(self):
            p = self.params()
            lines = engine.plan_lines(p["n_games"], p["n_fishing"], p["fishing_hours"],
                                      p["claim_every"], p["loops"], preview=3,
                                      order=p.get("order", "full_first"))
            tail = "" if p["loops"] else "　（无限轮：打完一轮直接接下一轮，不会自己停）"
            self.plan.setText("　".join(lines) + tail)

            # 合计：把「你填的数字 → 实际会打多少局」直接算出来，避免任何「默认帮你改成 6 局」的误会
            g, f, k, lp = p["n_games"], p["n_fishing"], p["claim_every"], p["loops"]
            h = p["fishing_hours"]
            if lp:
                tot_g, tot_f = g * lp, f * lp
                msg = (f"合计：{lp} 轮 × 每轮 {g} 局 = 总共 {tot_g} 局"
                       f"｜钓鱼 {tot_f} 次（每次 {h:g}h，共 {tot_f * h:g} 小时）")
                msg += (f"｜签到领券 {tot_g // k} 次（每累计 {k} 局一次）" if k
                        else "｜签到领券：已关闭")
            else:
                msg = (f"合计：无限轮 —— 每轮 {g} 局 + 钓鱼 {f} 次（{h:g}h），"
                       f"打完一轮立刻接下一轮，只有点「停止」才结束"
                       f"（想只跑固定局数：把「大循环轮数」填 1、2…）")
            # 签到按「累计局数」跨轮凑，某一轮会被拆成几段 —— 先说清楚，免得以为是 bug
            if k and g and (g % k):
                msg += (f"\n　注：签到是按累计局数跨轮算的（每 {k} 局一次），"
                        f"所以某轮会被拆成几段，但每轮总局数、总轮数都按你填的走；"
                        f"想每轮打满再签，把签到间隔填成 {g} 或它的倍数。")
            seq = ("先通关循环 → 后钓鱼" if p.get("order", "full_first") == "full_first"
                   else "先钓鱼模式 → 后通关")
            self.plan_sum.setText(f"顺序：{seq}｜" + msg)

        @safe
        def on_dry(self, show=True):
            """只看计划：点一下在计划区显示、再点收起（2026-11-21 18:32 用户要求）"""
            if not show:
                self.plan.setVisible(False)
                self.btn_dry.setText("只看计划")
                return
            p = self.params()
            lines = ["【计划预览】"]
            lines += ["  " + ln for ln in
                      engine.plan_lines(p["n_games"], p["n_fishing"], p["fishing_hours"],
                                        p["claim_every"], p["loops"], preview=5,
                                        order=p.get("order", "full_first"))]
            lines.append("  （只预览，没有真的开始）")
            self.plan.setText("\n".join(lines))
            self.plan.setVisible(True)
            self.btn_dry.setText("▾ 收起计划")

        @safe
        def on_start(self):
            if self.running:
                return
            # 调试开关在点「开始运行」时再同步一次环境变量：本次运行起的每个子进程都继承它
            self._debug_env(self.btn_dbg.isChecked())
            # 启动前自检：core 目录缺东西就直接说清楚（换电脑最常犯：只拷了 EXE）
            miss = [f for f in need_files()
                    if not os.path.exists(os.path.join(engine.core_dir(), f))]
            if miss:
                self.write(f"❌ core 目录不完整，缺：{', '.join(miss)}")
                QMessageBox.critical(
                    self, "文件不完整，没法开始",
                    f"解压不完整 —— 多半是只拷了 {EXE_LABEL}。\n\n"
                    "请把整个文件夹一起拷过去：里面必须有 core\\ 目录"
                    "（依赖库在 exe 同目录或 _internal\\ 里，两种布局都正常）。\n\n"
                    f"当前缺少：{', '.join(miss)}")
                crash_log("启动自检失败", f"core={engine.core_dir()} 缺 {miss}")
                return
            self.write(f"▶ 开始运行 {time.strftime('%Y-%m-%d %H:%M:%S')}"
                       f"｜版本 {APP_VER}｜{'打包EXE' if engine.FROZEN else '源码'}")
            # ---- 系统识别（2026-11-25 用户要求）----
            # 「点开始运行就检测本机是 W7 还是 W10」：这两个系统的 DPI 感知、后台抓图、
            # 响应速度都不一样，先判定再按档位取参数（configs/os_profile.json）。
            try:
                if engine.core_dir() not in sys.path:
                    sys.path.insert(0, engine.core_dir())
                import sysenv
                si = sysenv.info(refresh=True)
                sysenv.dump()                     # logs/sysenv.json，发日志时一起带上
                self.write(f"🖥 本机系统：{sysenv.summary()}")
                self.write(f"   按「{si['profile']}」档运行：{sysenv.describe_effect()}")
                if si["profile"] == "win7":
                    self.write("   ℹ Win7：悬停/等前台已放宽；后台抓图若黑屏会自动退回屏幕抓取")
                    if not si.get("admin"):
                        self.write("   ⚠ Win7 且非管理员：模拟按键/全局热键可能不生效，"
                                   "建议右键「以管理员身份运行」")
            except Exception as e:
                self.write(f"（系统识别失败，按默认参数运行：{e!r}）")
            # 启动前先看一眼：如果平台已经在登录界面，直接拦下（不让它开始乱点）
            try:
                if engine.core_dir() not in sys.path:
                    sys.path.insert(0, engine.core_dir())
                import login_guard as lg
                hit = lg.check("启动前", quiet=True)
            except Exception:
                hit = None
            if hit:
                rsn = hit.get("reason") or ("检测到账号被顶下线（异地登录）"
                                           if hit.get("kind") == "kicked_offline"
                                           else "检测到登录窗口")
                # 拦下来也要在界面留痕：时间 + 日志文件（用户要求「发生时间要能查到」）
                self.write("\n".join([
                    "=" * 56,
                    f"⛔ 启动前拦截：{rsn}  {hit.get('ts')}",
                    f"   已记入 {os.path.join(engine.logs_dir(), 'login_events.jsonl')}",
                    "   没有开始任务，也没有点任何东西。",
                    "=" * 56,
                ]))
                self.state.setText(f"⛔ {rsn} {hit.get('ts')}（启动前拦截）")
                QMessageBox.warning(
                    self, "不能开始",
                    f"{rsn}（{hit.get('ts')}），不能开始。\n\n"
                    f"这个时间已经记进日志 login_events.jsonl。\n\n"
                    f"请先手动登录，再点「开始运行」。")
                return
            p = self.params()
            if p["n_games"] <= 0 and p["n_fishing"] <= 0:
                QMessageBox.warning(self, "参数不对", "通关局数和钓鱼次数不能都为 0。")
                return
            save_cfg(p)
            # 透传给 full_loop → pipeline：卡「您正在游戏中」时等多久
            os.environ["KK_BUSY_WAIT_MIN"] = str(int(p.get("busy_wait", 6)))
            os.environ["KK_BUSY_ATTEMPTS"] = str(int(p.get("busy_attempts", 4)))
            os.environ["KK_RESTART_ON_ENTER_FAIL"] = "1" if p.get("busy_restart", True) else "0"
            os.environ["KK_MAX_ROUND_MIN"] = str(int(p.get("max_round_min", 50)))
            self.running = True
            self.stats = dict(games=0, cleared=0, fishing=0, claims=0, loop=0, task='通关', by_loop={})
            self._save_stats()     # 新一次运行 → 清零并落盘
            try:
                if self.btn_stats.isChecked():
                    self._refresh_stats()
            except Exception:
                pass
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self.sp_games.setEnabled(False)
            self.sp_fish.setEnabled(False)
            self.sp_hours.setEnabled(False)
            self.sp_claim.setEnabled(False)
            self.sp_loops.setEnabled(False)
            self.cb_order.setEnabled(False)
            self.cb_fail.setEnabled(False)
            self.sp_busy.setEnabled(False)
            self.sp_attempts.setEnabled(False)
            self.cb_busy_restart.setEnabled(False)
            self.sp_stuck.setEnabled(False)
            self.badge.setText("运行中")
            self.badge.setProperty("on", True)
            self.badge.style().unpolish(self.badge)
            self.badge.style().polish(self.badge)
            self.t0 = time.time()
            self.timer.start()

            self.thread = LoopThread(p)
            self.thread.line.connect(self.write)
            self.thread.state.connect(lambda s: self.state.setText(f"当前阶段：{s}"))
            self.thread.rc.connect(self.on_done)
            self.thread.start()

        @safe
        def on_stop(self):
            if not self.running:
                return
            self.btn_stop.setEnabled(False)
            self.write("⛔ 正在停止（会杀掉当前子进程树）…")
            self.state.setText("当前阶段：正在停止")
            if self.thread:
                self.thread.stop()

        @safe
        def on_done(self, rc):
            self.running = False
            pass
            self.timer.stop()
            self.lbl_time.setText("")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            for w in (self.sp_games, self.sp_fish, self.sp_hours,
                      self.sp_claim, self.sp_loops, self.cb_order, self.cb_fail,
                      self.sp_busy, self.sp_attempts, self.cb_busy_restart, self.sp_stuck):
                w.setEnabled(True)
            login_ts = ""
            login_reason = "检测到登录窗口"
            try:
                if self.thread and self.thread.loop:
                    login_ts = self.thread.loop.login_hit or ""
                    login_reason = self.thread.loop.login_reason or login_reason
            except Exception:
                pass
            if rc == engine.RC_LOGIN:
                # 登录窗口 / 账号被顶下线 → 只停手，不做任何操作（用户 2026-11-17 规则）
                short = "账号被顶下线" if "顶下线" in login_reason else "检测到登录窗口"
                self.badge.setText(short)
                self.badge.setProperty("on", False)
                extra = ("\n\n⚙️ 这是程序自己重启平台后弹出的登录（会话卡死只能重启平台清掉）。\n"
                         "平台现在是刚重开的，登录好再点「开始运行」即可。"
                         if "自动重启" in login_reason else "")
                self.badge.style().unpolish(self.badge)
                self.badge.style().polish(self.badge)
                self.state.setText(f"⛔ {login_reason} {login_ts}")
                self.write("\n".join([
                    "=" * 56,
                    f"⛔ {login_reason}：{login_ts}",
                    "   任务已停止，未做任何操作（没点登录/确定、没关窗口、没退游戏）",
                    f"   记录：{os.path.join(engine.logs_dir(), 'login_events.jsonl')}",
                    "=" * 56,
                    f"—— 循环结束 exit={rc} 总用时 {self.elapsed()} ——",
                ]))
                # 用户 2026-11-18 21:25：这个「检测到登录窗口」对话框删掉 —— 时间、原因、
                # 记录文件已经写在状态栏和界面日志里，弹出框只会挡着手动登录的操作。
                # extra（程序重启平台后弹登录的说明）改成一行日志，信息不丢。
                if extra:
                    self.write("   说明：" + extra.strip().replace("\n\n", "\n           "))
                return
            if rc == engine.RC_BUSY:
                # 僵尸会话：等过 N 分钟平台仍不放行 → 只能重启平台（用户 2026-11-17 12:20）
                self.badge.setText("请重启平台")
                self.badge.setProperty("on", False)
                self.badge.style().unpolish(self.badge)
                self.badge.style().polish(self.badge)
                self.state.setText("⛔ 平台会话卡住（自动重启后仍是「您正在游戏中」）")
                self.write("\n".join([
                    "=" * 56,
                    "⛔ 平台一直是「您正在游戏中，不能创建游戏房间」",
                    "   已自动走完：点确定 → 等设定分钟 × 3 次尝试 → 自动重启平台",
                    "   重启后仍不放行，说明平台侧会话没被清掉。",
                    "   处理办法：① 关掉 KK 官方对战平台  ② 重新打开并登录  ③ 点「开始运行」",
                    "=" * 56,
                    f"—— 循环结束 exit={rc} 总用时 {self.elapsed()} ——",
                ]))
                QMessageBox.warning(
                    self, "请重启平台",
                    "平台一直是「您正在游戏中，不能创建游戏房间」。\n\n"
                    "已经自动试过：点「确定」→ 等设定时间 × 3 次尝试 → 自动重启平台，"
                    "重开后仍然不放行。\n\n"
                    "这一层只能手动来：① 关掉 KK 官方对战平台  ② 重新打开并登录  "
                    "③ 再点「开始运行」。")
                return
            self.badge.setText("待机")
            self.badge.setProperty("on", False)
            self.badge.style().unpolish(self.badge)
            self.badge.style().polish(self.badge)
            self.state.setText("当前阶段：待机")
            self.write(f"—— 循环结束 exit={rc} 总用时 {self.elapsed()} ——")

        def elapsed(self) -> str:
            if not self.t0:
                return ""
            s = int(time.time() - self.t0)
            return f"{s // 3600}时{s % 3600 // 60}分{s % 60}秒"

        @safe
        def tick(self):
            self.lbl_time.setText(f"已运行 {self.elapsed()}")
            # 2026-11-21 18:44 用户要求：「本轮耗时」实时登记（每秒刷新）
            try:
                if getattr(self, "running", False) and self.stats.get("by_loop"):
                    _b = self.stats["by_loop"].get(self.stats.get("loop") or 1) or {}
                    if _b.get("t0"):
                        _s = int(time.time() - _b["t0"])
                        self.stat_vals["dur"].setText(
                            f"{_s // 60}分{_s % 60:02d}秒" if _s >= 60 else f"{_s}秒")
            except Exception:
                pass

        def closeEvent(self, ev):
            if self.running:
                r = QMessageBox.question(self, "还在运行",
                                         "循环还在跑，确定退出并停止吗？",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if r != QMessageBox.Yes:
                    ev.ignore()
                    return
                self.thread.stop()
                self.thread.wait(8000)
            save_cfg(self.params())
            ev.accept()

    # 本进程已是 per-monitor DPI aware（文件顶部 _setup_dpi）。Qt5 默认按「设备像素」布局，
    # 在 125%/150% 的笔记本上界面会显得只有 2/3 大、字挤在一起 → 打开 Qt 的高 DPI 缩放，
    # 界面按逻辑像素排（看着正常），而截图/点击仍走物理像素（识别更准）。
    try:
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)

    # ---- 崩溃兜底：任何未捕获异常都写日志 + 弹框，不再让进程静默退出 ----
    def _excepthook(tp, val, tb):
        txt = "".join(traceback.format_exception(tp, val, tb))
        crash_log("未捕获异常（界面线程）", txt)
        try:
            last = f"{tp.__name__}: {val}"
            QMessageBox.critical(
                None, "程序出错（已记录，没有退出）",
                f"{last[:400]}\n\n"
                f"详细记录已写入：\n{os.path.join(diag_dir(), 'gui_crash.log')}\n\n"
                f"把这个文件发给开发者就能定位。")
        except Exception:
            pass

    sys.excepthook = _excepthook

    def _thread_hook(args):
        txt = "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                 args.exc_traceback))
        crash_log(f"后台线程异常：{getattr(args.thread, 'name', '?')}", txt)

    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass

    boot = boot_record()
    # 注意：这里不能先写 running，否则 Win 里读不到「上一次」的状态
    app.setStyleSheet("""
        QMainWindow, QWidget { background: #16181d; color: #e6e8ec; }
        #h1 { font-size: 20px; font-weight: 700; color: #ffffff; }
        #sub { color: #7d8590; font-size: 12px; margin-left: 8px; }
        #badge { color: #8a8f98; border: 1px solid #2c3038; border-radius: 10px;
                 padding: 2px 10px; font-size: 12px; }
        #badge[on="true"] { color: #27c93f; border-color: #27c93f; }
        #card { background: #1c1f26; border: 1px solid #262a33; border-radius: 10px; }
        #lab { color: #a8b0bb; font-size: 12px; }
        #plan { color: #6ea8fe; font-size: 12px; background: #14161a;
                border: 1px solid #232732; border-radius: 8px; padding: 8px 10px; }
        #mini { color: #b9c1cd; font-size: 11px; background: transparent;
                border: none; padding: 2px 2px; text-align: left; }
        #mini:hover { color: #c3cad6; }
        #psum { color: #e8c76a; font-size: 12px; font-weight: 600; background: #1a1710;
                border: 1px solid #3a3220; border-radius: 8px; padding: 7px 10px; }
        #statpanel { background: #16181c; border: 1px solid #2a2d33; border-radius: 10px; }
        #stathead { color: #9a9aa3; font-size: 13px; }
        #statlbl { color: #7d7d88; font-size: 11px; }
        #statval_loop { color: #e8e8ee; font-size: 22px; font-weight: 500; }
        #statval_games { color: #e8e8ee; font-size: 22px; font-weight: 500; }
        #statval_cleared { color: #5fcaa0; font-size: 22px; font-weight: 500; }
        #statval_fishing { color: #7db4f0; font-size: 22px; font-weight: 500; }
        #statval_claims { color: #e8c46a; font-size: 22px; font-weight: 500; }
        #statval_dur { color: #c98ae8; font-size: 22px; font-weight: 500; }
        #statrow { color: #c9c9d1; font-size: 12px; background: #1c1e22; border-radius: 6px; padding: 5px 10px; }
        #statrow_cur { color: #e8e8ee; font-size: 12px; background: #242018;
                        border: 1px solid #4a3d20; border-radius: 6px; padding: 5px 10px; }
        #statfoot { color: #8f8f99; font-size: 12px; }
        #statscroll QScrollBar:vertical { width: 14px; background: #15171a; border-radius: 7px; margin: 0px; }
        #statscroll QScrollBar::handle:vertical { background: #555a63; border-radius: 6px; min-height: 24px; }
        #statscroll QScrollBar::handle:vertical:hover { background: #6e747e; }
        #statscroll QScrollBar::add-line:vertical, #statscroll QScrollBar::sub-line:vertical { height: 0px; }
        #statscroll QScrollBar::add-page:vertical, #statscroll QScrollBar::sub-page:vertical { background: transparent; }
        #state { color: #c9d1d9; font-size: 12px; }
        #timer { color: #7d8590; font-size: 12px; }
        #hint { color: #6b7280; font-size: 11px; }
        QSpinBox, QDoubleSpinBox { background: #101216; border: 1px solid #2c3038;
                border-radius: 6px; padding: 4px 6px; color: #e6e8ec; min-height: 22px; }
        QSpinBox:disabled, QDoubleSpinBox:disabled { color: #6b7280; }
        QCheckBox { color: #a8b0bb; font-size: 12px; }
        QPushButton { background: #262a33; border: 1px solid #333844; border-radius: 8px;
                padding: 7px 16px; color: #dfe3ea; }
        QPushButton:hover { background: #2f343e; }
        QPushButton:disabled { color: #5b6270; background: #1b1e24; }
        QPushButton#start { background: #1f7a3a; border-color: #2b8f47; color: #ffffff;
                font-weight: 600; }
        QPushButton#start:hover { background: #2a8f47; }
        QPushButton#start:disabled { background: #24322a; color: #5b6270; }
        QPushButton#stop { background: #7a1f1f; border-color: #8f2b2b; color: #ffffff; }
        QPushButton#stop:hover { background: #8f2a2a; }
        QPushButton#stop:disabled { background: #2a1e1e; color: #5b6270; }
        QPushButton#packlog { background: #24331f; border: 1px solid #3f5a33; color: #cfe8b8; }
        QPushButton#packlog:hover { background: #2f4426; }
        /* 调试开关：开着时明显变色，避免忘了关还以为是程序变慢了 */
        QPushButton#dbg { background: #24222b; border: 1px solid #3d3849; color: #b9b0c6; }
        QPushButton#dbg:hover { background: #2e2b38; }
        QPushButton#dbg:checked { background: #5a3a7a; border: 1px solid #8a5fbf;
                color: #ffffff; font-weight: bold; }
        QPlainTextEdit#log { background: #0e1014; border: 1px solid #232732;
                border-radius: 8px; color: #cfd6e0; selection-background-color: #24405f; }
        /* 弹窗：QWidget 的深色字色会套到白色原生对话框上 → 白底白字看不见（2026-11-17 16:59 踩到），
           这里把 QMessageBox 强制成深色底 + 浅色字 */
        QMessageBox { background: #1c1f26; }
        QMessageBox QLabel { color: #e6e8ec; font-size: 12px; min-width: 360px; }
        QMessageBox QPushButton { background: #232732; border: 1px solid #333947;
                border-radius: 6px; color: #dfe3ea; padding: 5px 16px; min-width: 64px; }
        QMessageBox QPushButton:hover { background: #2c3140; }
    """)
    w = Win()
    w.show()

    # 自检用：--auto-start → 界面起来 1.5s 后自动点「开始运行」（远程验收/无人值守时用）
    if "--auto-start" in sys.argv:
        QTimer.singleShot(1500, w.on_start)

    # 自检用：--screenshot 图片路径 → 渲染一张界面图然后退出（打包后验证界面正常）
    shot = None
    if "--screenshot" in sys.argv:
        i = sys.argv.index("--screenshot")
        if i + 1 < len(sys.argv):
            shot = sys.argv[i + 1]

    if shot:
        def _grab():
            try:
                w.grab().save(shot)
                print(f"[screenshot] {shot}", flush=True)
            except Exception as e:
                print(f"[screenshot] 失败 {e!r}", flush=True)
            app.quit()

        QTimer.singleShot(900, _grab)

    # 自检用：--fake-run → 用 _sleeper.py 冒充通关循环，跑「开始 → 停止」全流程
    if "--fake-run" in sys.argv:
        _fake_shot = os.path.join(engine.logs_dir(), "gui_fake_run.png")
        engine.SEG_FULL = "_sleeper.py"
        w.sp_games.setValue(1)
        w.sp_fish.setValue(0)
        w.sp_claim.setValue(0)
        w.sp_loops.setValue(1)

        def _step1():
            w.on_start()

        def _step2():
            print("[fake-run] 点停止", flush=True)
            w.on_stop()

        def _step3():
            try:
                w.grab().save(_fake_shot)
                print(f"[fake-run] 界面图 {_fake_shot}", flush=True)
            except Exception as e:
                print(f"[fake-run] 截图失败 {e!r}", flush=True)
            app.quit()

        QTimer.singleShot(600, _step1)
        QTimer.singleShot(5000, _step2)
        QTimer.singleShot(9000, _step3)
    return app.exec()


if __name__ == "__main__":
    fix_stdio()
    if dispatch_script(sys.argv):
        pass
    else:
        raise SystemExit(gui_main())
