# -*- coding: utf-8 -*-
"""循环引擎：把三个固定流程（通关循环 / 钓鱼模式 / 签到领券）串成一条大循环。

命令行（mega_loop.py）和图形界面（app_gui.py）共用这一份实现，避免两套逻辑走偏。

一条大循环：
  第 N 轮
    ├─ 通关循环 ×N 局（每累计 K 局插一次【签到领券】，领完继续）
    └─ 钓鱼模式 ×M 次（每次静等 H 小时）

打包成 EXE 后的关键约定
------------------------
子进程统一用 `sys.executable + core/xxx.py + 参数` 启动：
  - 开发态：sys.executable 是 python.exe，正常跑 .py
  - 打包后：sys.executable 是 KKAuto.exe 自己，EXE 启动时发现第一个非选项参数是
    .py 文件，就会用内置解释器 runpy 跑它（见 app_gui.py 的 dispatch_script）。
好处：原有几十个脚本里 `subprocess.run([sys.executable, "game_auto.py", ...])`
这种写法一个字都不用改，打包后照样成立。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

FROZEN = bool(getattr(sys, "frozen", False))

# Windows 下创建子进程不弹黑框
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

SEG_FULL = "full_loop.py"
SEG_FISH = "fishing_mode.py"
SEG_CLAIM = "daily_claim.py"

# 特殊退出码：检测到登录窗口 → 已停手（不做任何操作）
RC_LOGIN = 131
RC_BUSY = 9        # 僵尸会话（您正在游戏中，不能创建游戏房间）等完仍未释放 → 只能重启平台


def app_dir() -> str:
    """EXE 所在目录（开发态就是项目目录）"""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def core_dir() -> str:
    """脚本 + templates + configs 所在目录

    开发态：项目根目录
    打包后：<exe 同级>/core
    """
    if FROZEN:
        return os.path.join(app_dir(), "core")
    return app_dir()


def logs_dir() -> str:
    d = os.path.join(core_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def script_path(name: str) -> str:
    return os.path.join(core_dir(), name)


def child_cmd(script: str, args=None) -> list[str]:
    """子进程命令行：python/exe + core/x.py + args"""
    return [sys.executable, script_path(script), *(args or [])]


def child_env() -> dict:
    """子进程环境：强制 UTF-8，避免 emoji 在 cp936 管道里炸掉"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def kill_tree(pid: int):
    """杀整棵进程树（子进程还会再开孙进程，比如 game_auto.py）"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def plan_lines(n_games: int, n_fishing: int, fishing_hours: float,
               claim_every: int, loops: int, preview: int = 3,
               order: str = "full_first") -> list[str]:
    """给界面/命令行打印用的「计划预览」

    order：本轮先跑哪一段 —— "full_first"（默认，先通关后钓鱼）
                             "fish_first"（先钓鱼后通关）
    """
    out = []
    n_games = max(0, n_games)
    n_fishing = max(0, n_fishing)
    claim_every = max(0, claim_every)
    loops = max(0, loops)
    fish_first = str(order) == "fish_first"
    games_total = 0
    for lp in range(1, (loops or preview) + 1):
        game_parts = []
        remain = n_games
        while remain > 0:
            seg = min(claim_every - (games_total % claim_every), remain) if claim_every else remain
            game_parts.append(f"通关×{seg}")
            games_total += seg
            remain -= seg
            if claim_every and games_total % claim_every == 0:
                game_parts.append("签到领券")
        fish_parts = [f"钓鱼×1（{fishing_hours:g}h）" for _ in range(n_fishing)]
        parts = (fish_parts + game_parts) if fish_first else (game_parts + fish_parts)
        out.append(f"第 {lp} 轮：" + (" → ".join(parts) if parts else "（空）"))
    return out


class Loop:
    """一条大循环。on_line(str) 收日志、on_state(str) 收当前阶段。

    stop() 可从别的线程调用：会杀掉当前子进程树，循环随即退出。
    """

    def __init__(self, n_games=3, n_fishing=1, fishing_hours=5.0, claim_every=3,
                 loops=0, stop_on_fail=False,
                 busy_wait=None, busy_attempts=None, busy_restart=None,
                 max_round_min=None, order="full_first",
                 settle_min=None,
                 on_line=print, on_state=None, ensure_detail=True):
        self.n_games = max(0, int(n_games))
        self.n_fishing = max(0, int(n_fishing))
        self.fishing_hours = float(fishing_hours)
        self.claim_every = max(0, int(claim_every))
        self.loops = max(0, int(loops))
        self.stop_on_fail = bool(stop_on_fail)
        # 界面上的四项「无法创建房间 / 单局超时」参数。None = 不传，由 full_loop 读配置/环境变量。
        # 2026-11-17 16:59 修：这四项原来只由 GUI 塞进环境变量，Loop 根本不收 →
        #   TypeError: Loop.__init__() got an unexpected keyword argument 'busy_wait'
        self.busy_wait = None if busy_wait is None else max(0, int(busy_wait))
        self.busy_attempts = None if busy_attempts is None else max(1, int(busy_attempts))
        self.busy_restart = None if busy_restart is None else bool(busy_restart)
        self.max_round_min = None if max_round_min is None else max(0, int(max_round_min))
        # 2026-11-20 23:53 修：GUI 加了「秘境挑战等待结束」（settle_min），engine 必须收，
        # 否则点「开始运行」必崩：TypeError: Loop.__init__() got an unexpected keyword argument 'settle_min'
        # 实际生效由 full_loop 自己读 gui_settings.json 完成（gui_settle_timeout），这里只存着。
        self.settle_min = None if settle_min is None else max(1, int(settle_min))
        # 每轮两段的先后：full_first（默认，先通关后钓鱼）/ fish_first（先钓鱼后通关）
        self.order = "fish_first" if str(order) == "fish_first" else "full_first"
        self.ensure_detail = ensure_detail
        self.on_line = on_line
        self.on_state = on_state or (lambda s: None)
        self.journal_path = os.path.join(logs_dir(), "mega_journal.jsonl")
        self._proc = None
        self._stop = False
        self.games_total = 0
        self.loop_index = 0
        # ---- 登录窗口守卫 ----
        self.login_hit = None          # 命中的时间字符串（界面上要显示）
        self.login_reason = None       # 「检测到登录窗口」/「检测到账号被顶下线（异地登录）」
        self._login_stop = False       # 是不是因为登录窗口才停的
        self._manual_stop = False      # 是不是人工点的停止（优先于登录窗判定）
        self._busy_stop = False        # 是不是因为僵尸会话（正在游戏中）才停的
        self._wd_stop = threading.Event()
        self._wd_thread = None
        self._lg = None

    # ---------- 控制 ----------
    def stop(self):
        self._manual_stop = True
        self._stop = True
        p = self._proc
        if p is not None and p.poll() is None:
            kill_tree(p.pid)

    @property
    def stopped(self):
        return self._stop

    # ---------- 日志 ----------
    def log(self, msg: str):
        self.on_line(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def journal(self, kind, **kw):
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind}
        rec.update(kw)
        try:
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def full_args(self, rounds: int) -> list[str]:
        """通关循环的命令行参数：界面那 4 项在这里透传给 full_loop.py。
        （None 的项不传，full_loop 继续用配置/环境变量里的值）"""
        args = ["--rounds", str(int(rounds))]
        if self.busy_wait is not None:
            args += ["--busy-wait", str(self.busy_wait)]
        if self.busy_attempts is not None:
            args += ["--busy-attempts", str(self.busy_attempts)]
        if self.busy_restart is False:
            args += ["--no-busy-restart"]
        if self.max_round_min is not None:
            args += ["--max-round-min", str(self.max_round_min)]
        return args

    # ---------- 跑一段 ----------
    def run_segment(self, script: str, args: list[str], label: str) -> int:
        if self._stop:
            return 130
        # 2026-11-19 21:40 用户定：子脚本自带守卫（full_loop/mega_loop）时暂停引擎
        # 这份重复轮询；钓鱼/签到没有常驻守卫，引擎守卫照常。见 _lg_set_paused。
        self._lg_set_paused(any(s in script for s in ("full_loop.py", "mega_loop.py")))
        # 每段开跑前再扫一次：登录窗出现了就一个动作都别做
        if self.check_login_now():
            return RC_LOGIN
        # 「首充礼包 连锁三天」这类广告浮层会盖住主窗、吃掉所有点击
        # （用户 2026-11-17 13:58：退出游戏/重开平台时随机弹，直接点 X 关掉）
        try:
            import ad_guard
            if ad_guard.close(verbose=False) == "closed":
                self.log("   🧹 关掉了首充/活动广告浮层（会挡住下面所有点击）")
        except Exception:
            pass
        # 独立窗口型的活动弹窗（「我要变强！」754x626 之类）：用户 2026-11-17 17:31
        # 要求「检测到第一时间优先处理」，所以每段开跑前也扫一遍
        try:
            import popup_guard
            n = popup_guard.close_popups(max_n=3, quiet=True)
            if n:
                self.log(f"   🧹 关掉了 {n} 个活动弹窗（我要变强/首充礼包之类）")
        except Exception:
            pass
        self.on_state(label)
        self.log(f"▶ {label}")
        self.log(f"   {script} {' '.join(args)}".rstrip())
        t0 = time.time()
        rc = 1
        try:
            p = subprocess.Popen(
                child_cmd(script, args), cwd=core_dir(),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, env=child_env(),
                creationflags=CREATE_NO_WINDOW)
            self._proc = p
            assert p.stdout is not None
            for line in p.stdout:
                self.on_line(line.rstrip("\n"))
                if self._stop:
                    kill_tree(p.pid)
                    break
            rc = p.wait()
        except Exception as e:
            self.log(f"   ❌ 启动失败：{e!r}")
            rc = 1
        finally:
            self._proc = None
        dur = int(time.time() - t0)
        mark = "✅" if rc == 0 else ("⛔" if self._login_stop else "❌")
        self.log(f"   {mark} {label} 结束 exit={rc} "
                 f"用时 {dur // 60}分{dur % 60}秒")
        self.journal("segment", label=label, script=script, args=args,
                     rc=rc, seconds=dur)
        # 自动重启平台后又撞上「登录 / 顶号」→ 立刻停手（绝不自动登录）
        if rc == RC_LOGIN and not self._login_stop and not self._stop:
            self._login_stop = True
            self.login_reason = "检测到登录窗口（自动重启平台后）"
            self.login_hit = time.strftime("%Y-%m-%d %H:%M:%S")
            self._stop = True
            self.on_state(f"⛔ {self.login_reason} {self.login_hit}")
            self.log("⛔ 自动重启平台后弹出登录窗口 → 停手，不点任何东西")
            self.log("   手动登录好之后，再点「开始运行」")
            self.journal("login_window", reason=self.login_reason, loop=self.loop_index)
            return rc
        # 僵尸会话：等过 N 分钟仍然「您正在游戏中，不能创建游戏房间」→ 只能重启平台
        if rc == RC_BUSY and not self._stop:
            self._busy_stop = True
            self._stop = True
            self.on_state("⛔ 平台会话卡住（您正在游戏中）")
            self.log("⛔ 平台一直不让建房：游戏会话卡在平台进程里，等完仍未释放")
            self.log("   请手动：关掉 KK 官方对战平台 → 重新打开登录 → 再点「开始运行」")
            self.journal("busy_session", label=label)
            return rc
        return rc

    # ---------- 登录窗口守卫 ----------
    def _start_login_watch(self):
        """后台每 3 秒扫一次登录窗口；命中 → 只停手，不点任何东西（2026-11-17 用户规则）

        2026-11-19 21:40 用户定「重复就删除」：每次启动都新建停止 Event，
        这样暂停（_lg_set_paused(True)）之后可以随时重启一份新线程。
        """
        self._wd_stop = threading.Event()      # 新建：旧线程用旧 Event，set 旧的停旧的
        try:
            if core_dir() not in sys.path:
                sys.path.insert(0, core_dir())
            import login_guard as lg
        except Exception as e:
            self.log(f"   (登录窗口守卫没启用：{e!r})")
            return
        self._lg = lg

        def on_hit(rec):
            self.login_hit = rec.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S")
            self.login_reason = lg.reason_of(rec)
            self._login_stop = True
            self.log(f"⛔ {self.login_reason} → 立刻停止任务（不做任何操作）")
            self.on_state(f"⛔ {self.login_reason} {self.login_hit}")
            self.journal("kicked_offline" if rec.get("kind") == "kicked_offline"
                         else "login_window", ts_hit=self.login_hit,
                         reason=self.login_reason,
                         windows=rec.get("windows"), loop=self.loop_index)
            # 只杀自己起的自动化子进程树，绝不碰平台/游戏窗口
            self._stop = True
            p = self._proc
            if p is not None and p.poll() is None:
                kill_tree(p.pid)

        self._wd_thread = threading.Thread(
            target=lg.watch_loop,
            args=(self._wd_stop, on_hit, 3.0, f"第 {self.loop_index or 1} 轮"),
            daemon=True, name="login-guard")
        self._wd_thread.start()
        self.log("🛡 登录窗口守卫已启动（每 3s 扫一次；命中即停手，不做任何操作）")

    def _lg_set_paused(self, paused: bool):
        """跑自带守卫的子脚本（full_loop.py / mega_loop.py）时暂停引擎这份守卫。

        2026-11-19 21:40 用户定「重复就删除」：GUI 跑通关/大循环段时，子进程
        （full_loop/mega_loop）自己就带一个 3 秒守卫线程，与引擎这份职责完全相同；
        而引擎这份跑在 GUI 进程里（Qt 抓图慢）任务期间会持续多吃一块 CPU。
        其余脚本（fishing_mode / daily_claim 没有常驻守卫线程）引擎守卫照常，
        不留空窗。段结束下一次 run_segment 开头会按脚本名自动恢复状态。
        """
        if paused:
            if (self._wd_thread and self._wd_thread.is_alive()
                    and not self._wd_stop.is_set()):
                self._wd_stop.set()
                self.log("🛡 引擎守卫已暂停（该子脚本自带守卫线程接管，避免重复轮询）")
        else:
            if (self._wd_thread is None or not self._wd_thread.is_alive()
                    or self._wd_stop.is_set()):
                self._start_login_watch()

    def check_login_now(self) -> bool:
        """段边界再补扫一次（比 3s 轮询更及时）。命中返回 True"""
        if self._login_stop:
            return True
        lg = self._lg
        if lg is None:
            return False
        try:
            rec = lg.check(f"第 {self.loop_index} 轮 · 段边界")
        except Exception:
            return False
        if rec:
            self.login_hit = rec.get("ts")
            self.login_reason = lg.reason_of(rec)
            self._login_stop = True
            self.on_state(f"⛔ {self.login_reason} {self.login_hit}")
            self.journal("kicked_offline" if rec.get("kind") == "kicked_offline"
                         else "login_window", ts_hit=self.login_hit,
                         reason=self.login_reason,
                         windows=rec.get("windows"), loop=self.loop_index)
            self._stop = True
            p = self._proc
            if p is not None and p.poll() is None:
                kill_tree(p.pid)
            return True
        return False

    # ---------- 回详情页 ----------
    def ensure_detail_page(self):
        if not self.ensure_detail:
            return
        try:
            if core_dir() not in sys.path:
                sys.path.insert(0, core_dir())
            import fishing_mode as fm
            fm.ensure_detail_page()
        except Exception as e:
            self.log(f"   (回详情页跳过：{e!r})")

    # ---------- 两段（先后由 self.order 决定） ----------
    def _seg_games(self) -> int:
        """通关循环 ×n_games 局，每累计 claim_every 局插一次【签到领券】"""
        remain = self.n_games
        while remain > 0 and not self._stop:
            if self.claim_every:
                seg = min(self.claim_every - (self.games_total % self.claim_every), remain)
            else:
                seg = remain
            # 2026-11-23 00:42 用户要求：任务中断/失败 → 重复上次任务，正常完成才进下一局
            rc = 1
            for _att in range(3):
                rc = self.run_segment(
                    SEG_FULL, self.full_args(seg),
                    f"通关循环 ×{seg} 局（累计 {self.games_total + seg} 局）")
                if rc == 0 or self._stop or self.stop_on_fail:
                    break
                self.log(f"⚠️ 通关循环失败（exit={rc}）→ 重跑同一段（第 {_att + 1}/3 次）")
            self.games_total += seg
            remain -= seg
            if self._stop:
                return 0
            if self.stop_on_fail and rc != 0:
                self.log("⛔ 已勾选「失败即停」：通关循环失败，停止")
                return rc
            self.check_login_now()
            if self._stop:
                return 0
            if self.claim_every and self.games_total % self.claim_every == 0:
                self.log(f"💠 累计 {self.games_total} 局 → 执行【签到领券】")
                # 刚打完局，平台可能还停在结算/战绩页，先拉回详情页再签到领券
                if self.check_login_now():
                    return 0
                self.ensure_detail_page()
                time.sleep(2.0)
                rc2 = self.run_segment(SEG_CLAIM, [], "签到领券")
                if self.stop_on_fail and rc2 != 0:
                    self.log("⛔ 已勾选「失败即停」：签到领券失败，停止")
                    return rc2
        return 0

    def _seg_fishing(self) -> int:
        """钓鱼模式 ×n_fishing 次"""
        for j in range(1, self.n_fishing + 1):
            if self._stop or self.check_login_now():
                return 0
            # 2026-11-23 01:17 用户要求：钓鱼中断 → 重跑这次钓鱼，最多 5 次
            rc = 1
            for _att in range(5):
                rc = self.run_segment(
                    SEG_FISH, ["--hours", f"{self.fishing_hours:g}"],
                    f"钓鱼模式 第 {j}/{self.n_fishing} 次（{self.fishing_hours:g} 小时）")
                if rc == 0 or self._stop or self.stop_on_fail:
                    break
                self.log(f"⚠️ 钓鱼模式失败（exit={rc}）→ 重跑这次（第 {_att + 1}/5 次）")
            if self.stop_on_fail and rc != 0:
                self.log("⛔ 已勾选「失败即停」：钓鱼模式失败，停止")
                return rc
        return 0

    # ---------- 主循环 ----------
    def run(self) -> int:
        self.log("=" * 56)
        self.log("【三大流程循环】")
        if self.order == "fish_first":
            _ord = (f"先钓鱼模式 {self.n_fishing} 次（每次 {self.fishing_hours:g}h）"
                    f" → 再通关循环 {self.n_games} 局")
        else:
            _ord = (f"先通关循环 {self.n_games} 局"
                    f" → 再钓鱼模式 {self.n_fishing} 次（每次 {self.fishing_hours:g}h）")
        self.log(f"  每轮：{_ord}")
        self.log(f"  签到领券：每累计 {self.claim_every if self.claim_every else '∞'} 局插一次"
                 + ("（关闭）" if not self.claim_every else ""))
        self.log(f"  大循环：{'无限' if not self.loops else f'{self.loops} 轮'}")
        self.log("=" * 56)
        self._start_login_watch()

        try:
            while not self.loops or self.loop_index < self.loops:
                if self._stop:
                    break
                self.loop_index += 1
                self.log("=" * 56)
                self.log(f"🔁 第 {self.loop_index} 轮"
                         f"{'（无限循环）' if not self.loops else f'/{self.loops}'} 开始")
                self.log("=" * 56)
                self.journal("loop_start", loop=self.loop_index)

                self.on_state("准备：回到地图详情页")
                if self.check_login_now():
                    break
                self.ensure_detail_page()

                # ---- 本轮两段：通关循环 / 钓鱼模式，谁先跑由界面「优先顺序」决定 ----
                if self.order == "fish_first":
                    rc = self._seg_fishing()
                    if rc:
                        return rc
                    if self._stop:
                        break
                    rc = self._seg_games()
                    if rc:
                        return rc
                else:
                    rc = self._seg_games()
                    if rc:
                        return rc
                    if self._stop:
                        break
                    rc = self._seg_fishing()
                    if rc:
                        return rc

                if self._stop:
                    break
                self.journal("loop_end", loop=self.loop_index, games_total=self.games_total)
                self.log(f"🔁 第 {self.loop_index} 轮结束（累计通关 {self.games_total} 局）")

            if self._stop:
                if self._busy_stop and not self._manual_stop:
                    self.on_state("⛔ 请重启平台（您正在游戏中）")
                    self.log("=" * 56)
                    self.log("⛔ 平台会话卡住：一直是「您正在游戏中，不能创建游戏房间」")
                    self.log("   已经等完了设定的重试时间，平台仍未释放游戏会话。")
                    self.log("   处理办法：① 关掉 KK 官方对战平台  ② 重新打开并登录  ③ 点「开始运行」")
                    self.log("=" * 56)
                    self.journal("busy_stop", games_total=self.games_total)
                    return RC_BUSY
                if self._login_stop and not self._manual_stop:
                    rsn = self.login_reason or "检测到登录窗口"
                    self.on_state(f"⛔ {rsn} {self.login_hit}")
                    self.log("=" * 56)
                    self.log(f"⛔ {rsn}：{self.login_hit}")
                    self.log("   已停止任务，未做任何操作（没点登录/确定、没关窗口、没退游戏）")
                    self.log(f"   记录文件：{os.path.join(logs_dir(), 'login_events.jsonl')}")
                    self.log("=" * 56)
                    self.journal("login_stop", ts_hit=self.login_hit,
                                 games_total=self.games_total)
                    return RC_LOGIN
                self.on_state("已停止")
                self.log("⛔ 已手动停止")
                self.journal("interrupted", games_total=self.games_total)
                return 130
            self.on_state("全部完成")
            self.log("🏁 全部轮次完成")
            return 0
        except KeyboardInterrupt:
            self.on_state("已中断")
            self.log("⛔ 收到中断")
            self.journal("interrupted", games_total=self.games_total)
            return 130
        finally:
            self._wd_stop.set()
