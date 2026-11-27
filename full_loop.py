# -*- coding: utf-8 -*-
"""
通关循环（一局 = A→B→C→D→E），把所有零散脚本归类成一条链。

  A 进场    enter   : 清场残留 War3 → 房间校验/复用 → pipeline 开局 → 等进游戏
  B 局内准备 prepare : 四步（技能 3 / 装备 4 / 升级武器 3 / 自动挑战）
  C 提前挑战 clear   : 双击紫色水晶图标 → 弹窗点「是」；挑战打完后接「挑战存档」按钮
  D 存档挑战 slots   : 12 格双击 × 2 轮（必须在秘境之前，游戏原文有要求）
  E 秘境挑战 secret  : 海岛挑战 / 不死挑战（勾选哪个打哪个，弹窗点「是」）
  F 收尾    finish   : 等结算界面 → 点「继续观战」→ 退出游戏
                       胜利/失败都认：「失败」大字是蓝白色（秘境或主游戏这场没打过），
                       照样点「继续观战」再退出（用户 2026-11-18 09:49 要求）。
                       想失败后继续等真正的胜利：configs/full_loop.json → stages.finish.on_fail=wait

阶段开关全部在 configs/full_loop.json 里，命令行参数可临时覆盖（软件里的勾选框就对应这些字段）。

用法：
  python full_loop.py                              # 按配置跑 1 轮
  python full_loop.py --rounds 3                   # 连跑 3 轮
  python full_loop.py --undead --no-island         # 这轮打不死（勾选覆盖）
  python full_loop.py --island --no-undead         # 这轮打海岛
  python full_loop.py --no-secret                  # 不打秘境（只做通关动作→等胜利→退出）
  python full_loop.py --only A,B                   # 只跑指定阶段
  python full_loop.py --skip E                     # 跳过某阶段（比如想留着游戏不退）
  python full_loop.py --dry                        # 只打印计划，一下不动
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
DEF_CFG = os.path.join(HERE, "configs", "full_loop.json")
DIFF_REC = os.path.join(HERE, "logs", "lobby_difficulty.json")
# 项目目录写保护 → game_auto 会兜底写到 %TEMP%/KKAuto/，读和清两边都要看
DIFF_ALT_DIR = os.path.join(os.environ.get("TEMP") or os.environ.get("TMPDIR") or ".", "KKAuto")
DIFF_REC_ALT = os.path.join(DIFF_ALT_DIR, "lobby_difficulty.json")
# 项目目录那份可能删不掉（宿主机写保护）→ 记一个「何时清过」的哨兵，
# 读取时把比它更早的记录视为已失效，别把上一轮的难度串到这一轮。
DIFF_CLEARED = os.path.join(DIFF_ALT_DIR, "lobby_difficulty.cleared")
DIFF_TARGET = os.path.join(HERE, "logs", "diff_target.json")

RISK_HIT = {"v": False}          # 本轮是否遇到「作弊风险」弹窗（遇到就杀 War3 → 重开同一轮继续）
BUSY_HIT = {"v": False}          # 是否遇到「您正在游戏中，不能创建游戏房间」等完仍未释放（只能重启平台）
BACK_HIT = {"v": False}          # 本轮是否遇到「游戏已经结束 + 返回平台」
BACK_TOKENS = ("[BACK_PLATFORM]", "返回平台")
LOGIN = {"ts": None, "guard": None}   # 检测到登录窗口的时间（命中就停手，不做任何操作）
SH_PROC = {"p": None}                 # 当前正在跑的子进程（登录窗出现时直接掐掉）
RC_LOGIN = 131
RC_DISC = 7                           # 断线重连弹窗：收尾后本轮作废，让引擎重新开局
RC_BUSY = 9                           # 僵尸会话（您正在游戏中，不能创建游戏房间）等完仍未释放
RC_RESTARTED = 10                     # 平台刚被自动重启 → 整段从第 1 步重跑一次
RC_NODLG = 11                         # 「创建房间」连点 3 次建房子弹窗没出来 → 整段从第 1 步重跑一次
RC_BUSY_RETRY = 13                    # 命中「您正在游戏中」→ 点确定 + 等完 → 从第 1 步整段重来（次数由这里数）

# 单局看门狗：一轮（一次通关循环）从开头算起超过 limit 分钟还没打完 → 判卡死。
# 用户 2026-11-17 15:20：这一条**不再算「平台重启」的触发条件** —— 卡死只杀 War3 + 重开游戏 +
# 重跑本轮（与风控共用 risk_retry 次数），平台不动。平台重启只剩「无法创建房间」一条。
# limit 由配置 max_round_minutes / 环境变量 KK_MAX_ROUND_MIN 给；sh() 用它收紧子进程超时。
ROUND = {"limit": 50, "deadline": None}
ROUND_HIT = {"v": False}              # 本轮是否因「超过 N 分钟没打完」被掐断


def login_watch_start():
    """后台盯登录窗口：命中只设标志 + 掐掉当前子进程，绝不点任何东西"""
    try:
        import login_guard as lg
    except Exception as e:
        print(f"   (登录窗口守卫没启用：{e!r})", flush=True)
        return

    import threading

    def on_hit(rec):
        if LOGIN["ts"]:
            return
        LOGIN["ts"] = rec.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S")
        p = SH_PROC.get("p")
        if p is not None and p.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True)
            except Exception:
                pass

    login_watch_stop()          # 防重：④ 段回来重启时，先把旧线程停掉再起新的
    ev = threading.Event()
    LOGIN["guard"] = ev
    threading.Thread(target=lg.watch_loop,
                     args=(ev, on_hit, 3.0, "通关循环"),
                     daemon=True, name="login-guard").start()
    print("🛡 登录窗口守卫已启动（命中即停手，不做任何操作）", flush=True)


def login_watch_stop():
    """暂停主进程守卫线程（2026-11-19 20:00 用户定）。

    ④ 段期间守卫由 watch_early（游戏监控）承担，主进程这份要停掉，
    否则同一时段两个进程各跑一个守卫轮 = 双监控并存。出 ④ 由 login_watch_start 恢复。
    """
    ev = LOGIN.get("guard")
    if ev is not None and not ev.is_set():
        ev.set()
        print("🛡 主进程守卫已暂停 → ④ 段期间由游戏监控 watch_early 承担", flush=True)


def check_login(label="") -> bool:
    """段边界补扫一次。命中返回 True（已记录时间）"""
    if LOGIN["ts"]:
        return True
    try:
        import login_guard as lg
    except Exception:
        return False
    rec = lg.check(label or "通关循环")
    if rec:
        LOGIN["ts"] = rec.get("ts")
        return True
    return False


def wait_quiet(seconds: int, why: str = "", verbose: bool = True) -> bool:
    """精确等待 N 秒，期间每 10 秒扫一次登录窗。返回 True = 命中登录窗（调用方立刻停手）。

    2026-11-17 17:18 实测踩到：原来写的是「每秒 sleep(1) + 每秒 check_login」，
    而 check_login 每次要枚举窗口 + 截图（约 1.8s），于是「等 30 秒」实际耗了 84 秒，
    秘境挑战被硬推迟近一分钟。这里改成按截止时间算，登录检查降到 10 秒一次。
    """
    seconds = max(0, int(seconds))
    if seconds <= 0:
        return False
    t_end = time.time() + seconds
    next_chk = 0.0
    last_shown = None
    while True:
        left = t_end - time.time()
        if left <= 0:
            break
        now = int(round(left))
        if (verbose and now > 0 and now % 10 == 0
                and now < seconds and now != last_shown):
            print(f"      …还等 {now}s", flush=True)
            last_shown = now
        if time.time() >= next_chk:
            if check_login(why or "等待期间"):
                return True
            next_chk = time.time() + 10
        time.sleep(min(0.5, max(0.05, left)))
    return False


def login_abort(where=""):
    ts = LOGIN["ts"] or time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        import login_guard as lg
        rsn = lg.last_reason()
    except Exception:
        rsn = "检测到登录窗口"
    print("", flush=True)
    print("=" * 62, flush=True)
    print(f"⛔ {rsn}：{ts}", flush=True)
    print(f"   位置：{where or '通关循环'}", flush=True)
    print("   已停止任务，未做任何操作（没点登录/确定、没关窗口、没退游戏、没重开）", flush=True)
    print("   记录：logs/login_events.jsonl", flush=True)
    print("=" * 62, flush=True)
    return RC_LOGIN


def kill_war3_and_wait(verbose: bool = True) -> bool:
    """遇到风控：把 War3 彻底结束，并等到进程真没了。

    用户 19:06 要求：不再「遇到就收工」，改成**杀掉 War3 → 重开游戏 → 接着跑**。
    复用 cheat_guard.kill_war3()（taskkill /F /T），失败时退回裸 taskkill，
    最后再确认一遍进程确实清了，避免下一轮 A 阶段带着半个旧进程去开局。
    """
    for attempt in range(3):
        try:
            import cheat_guard as cg
            left = cg.kill_war3(verbose=verbose and attempt == 0)
            if not left:
                return True
        except Exception as e:
            if verbose and attempt == 0:
                print(f"   ⚠ cheat_guard 结束 War3 失败（{e!r}）→ 退回 taskkill", flush=True)
            for exe in ("War3.exe", "Warcraft III.exe"):
                # 不接 text=True：taskkill 的输出是 GBK，按 utf-8 解码会抛异常
                subprocess.run(["taskkill", "/F", "/IM", exe, "/T"], capture_output=True)
            time.sleep(1.5)
            try:
                import cheat_guard as cg
                if not cg.war3_windows():
                    return True
            except Exception:
                return True
        time.sleep(1.0)
    return False


def restart_platform(why="", verbose=True):
    """平台重启的唯一入口（用户 2026-11-17 14:45 定，15:03 缩为两类用途）：

      ① 无法创建房间（「您正在游戏中，不能创建游戏房间」/ 建房子弹窗不出来）
      ② 通关循环超过 N 分钟还没打完一局（默认 50 分钟）

    「无法正常开始游戏」（点了开始没进地图 / 开局步骤失败）用户 15:03 明确删除，不重启平台。

    顺序：War3 在跑先强杀（否则 plat_restart 拒绝重启，会把这一局连同存档弄没）
         → 收掉「断线重连 / 断线重连失败」弹窗
         → 关平台（taskkill）→ 重开 → 等大厅。
    返回 (ok, status)，status 与 plat_restart.restart() 一致（ok/login/war3/nowin/noexe/fail）。
    """
    if verbose:
        print(f"\n♻️ 重启平台：{why}", flush=True)
    if find_war3():
        if verbose:
            print("   ⛔ War3 还在跑 → 先强杀（不杀的话平台拒绝重启）", flush=True)
        kill_war3_and_wait(verbose=False)
        time.sleep(1.5)
        # 强杀之后常冒「断线重连 / 断线重连失败」——按第四十一节规则收掉，
        # 否则重启完平台还是被弹窗挡着，大厅永远判不就绪。
        try:
            import disc_guard as dg
            for _ in range(3):
                # ESC 关掉也要继续查下一个（可能还挂着「断线重连失败」提示）
                if dg.dismiss(verbose=verbose) in ("esc", "clicked"):
                    time.sleep(0.8)
                    continue
                if dg.dismiss_fail(verbose=verbose) in ("esc", "clicked"):
                    time.sleep(0.8)
                    continue
                break
        except Exception as e:
            if verbose:
                print(f"   (断线弹窗收尾跳过：{type(e).__name__}: {e})", flush=True)
    try:
        import plat_restart as pr
    except Exception as e:
        if verbose:
            print(f"   ❌ 重启模块加载失败：{e!r}", flush=True)
        return False, "norestart"
    rr = pr.restart(verbose=verbose)
    st = rr.get("status")
    if st == "ok":
        if verbose:
            print(f"   ✅ 平台已重开（{rr.get('seconds', '?')}s，"
                  f"大厅{'已就绪' if rr.get('lobby') else '未确认'}）", flush=True)
        return True, st
    if verbose:
        print(f"   ❌ 自动重启平台没成功：{st}", flush=True)
    return False, st


ORDER = ["enter", "prepare", "clear", "slots", "secret", "finish"]
TAG = {"enter": "A", "prepare": "B", "clear": "C",
       "slots": "D", "secret": "E", "finish": "F"}


# ---------------------------------------------------------------- 难度
def parse_num(name):
    m = re.match(r"[Nn]?\s*(\d+)", str(name or ""))
    return int(m.group(1)) if m else None


def diff_of(name, src, low_max=7):
    """把一个难度名（N3 / 3）解析成 (名称, 数字, 是否低难度, 来源)"""
    num = parse_num(name)
    return name, num, (None if num is None else num <= low_max), src


def build_seq(root, args, rounds):
    """本轮该选哪个难度，返回长度 = 轮数 的列表（逐轮 N1..N6）。

    优先级：--difficulty-seq "N1,N2,..." > 配置 difficulty.seq > 空（沿用旧逻辑）
    """
    raw = getattr(args, "difficulty_seq", "") or ""
    lst = [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()]
    if not lst:
        lst = [str(x) for x in ((root.get("difficulty", {}) or {}).get("seq") or [])]
    if not lst:
        return []
    if rounds <= 0:          # 无限模式：只预生成一轮循环的长度，之后沿用最后一个
        return list(lst)
    return [lst[min(i, len(lst) - 1)] for i in range(rounds)]


def write_diff_target(row, name, tag):
    """告诉游戏脚本「本轮点第几行」——写 logs/diff_target.json，game_auto 会读它"""
    try:
        os.makedirs(os.path.dirname(DIFF_TARGET), exist_ok=True)
        with open(DIFF_TARGET, "w", encoding="utf-8") as f:
            json.dump({"row": int(row), "name": name, "tag": tag,
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"   ⚠ 写 logs/diff_target.json 失败：{e}", flush=True)
        return False


_DIFF_RM_FAILED = [False]   # 2026-11-19 20:56 用户定：主路径删过一次失败就不再试 ——
                            # 宿主机文件代理每次 os.remove 都会刷一行 SAFE_DELETE_FAIL_CLOSED，
                            # 旧文件反正删不掉、读取又有时间哨兵兜底，反复删只是刷屏。

def clear_diff_rec():
    """清掉上一轮的难度记录，避免这轮没选成难度时读到旧值（串味）

    项目目录可能被宿主机写保护（删不动），所以 %TEMP% 兜底那份也要清；
    万一项目目录那份删不掉，再落一个时间哨兵，读取时把更早的记录当失效。
    主路径失败一次后本进程内跳过（哨兵保证不串味），%TEMP% 兜底那份照常清。
    """
    for p in (DIFF_REC, DIFF_REC_ALT):
        if p == DIFF_REC and _DIFF_RM_FAILED[0]:
            continue
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            if p == DIFF_REC:
                _DIFF_RM_FAILED[0] = True
    try:
        os.makedirs(DIFF_ALT_DIR, exist_ok=True)
        with open(DIFF_CLEARED, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass


def clear_diff_target():
    """清掉残留的难度目标（自动模式用：不指定行号，让大厅既然有新高难度就点最高档）"""
    try:
        if os.path.exists(DIFF_TARGET):
            os.remove(DIFF_TARGET)
            return True
    except Exception as e:
        print(f"   \u26a0 清残留难度目标失败：{e}", flush=True)
    return False


def read_diff_rec():
    """读大厅选完难度后的落盘记录（picked_index / hl_index / read_digit / verified）

    项目目录和 %TEMP% 兜底两份都看：跳过「清空哨兵」之前的老记录，在多份里取 ts 最新那份。
    这样即便项目目录那份因写保护删不掉，也不会把上一轮的难度串到本轮。
    """
    cleared = ""
    try:
        if os.path.exists(DIFF_CLEARED):
            with open(DIFF_CLEARED, encoding="utf-8") as f:
                cleared = (f.read() or "").strip()
    except Exception:
        cleared = ""
    best, best_ts = {}, ""
    for p in (DIFF_REC, DIFF_REC_ALT):
        try:
            with open(p, encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        ts = str(j.get("ts") or "")
        if not ts:
            continue
        if cleared and ts < cleared:
            continue                       # 这份是「清之前」的老记录，作废
        if ts > best_ts:
            best, best_ts = j, ts
    return best


# ---------------------------------------------------------------- 秘境交替
# 用户 2026-11-16 21:09：N8 及以上打秘境挑战，第一局海岛、第二局不死、第三局海岛……循环。
# 状态落盘在 logs/secret_alt.json，所以「一次只跑 1 局」的分散启动也能接着上一次的节奏走。
ALT_STATE = os.path.join(HERE, "logs", "secret_alt.json")
# 兜底路径：项目装在只读目录（Program Files / 本机写保护）时 logs/ 写不进去，
# 实测后果很严重 —— alt_save 失败 → last 永远不更新 → 三局连跑都打同一个秘境
# （2026-11-18 03:50 实测：3 局全打「不死」，交替完全失效）。
ALT_STATE_FALLBACK = os.path.join(tempfile.gettempdir(), "KKAuto", "secret_alt.json")
ALT_ALIAS = {"海岛": "island", "海島": "island", "island": "island",
             "不死": "undead", "不死族": "undead", "undead": "undead",
             "三角洲": "delta", "delta": "delta"}
ALT_CN = {"island": "海岛", "undead": "不死", "delta": "三角洲"}


def alt_seq_of(root):
    """读 stages.secret.alt → 交替序列（如 ['island','undead']），未启用返回 None"""
    scfg = (root.get("stages", {}) or {}).get("secret", {}) or {}
    a = scfg.get("alt")
    if not isinstance(a, dict) or not bool(a.get("enabled", True)):
        return None
    raw = a.get("seq") or ["island", "undead"]
    seq = [ALT_ALIAS[str(x).strip().lower()] for x in raw
           if str(x).strip().lower() in ALT_ALIAS]
    return seq or None


def alt_last():
    """上一次真打的秘境类型（island / undead），没有记录返回 None

    先读项目 logs/，再读 %TEMP% 兜底；两处都取「较新的 ts」，避免主路径残留旧记录
    把兜底里更新的节奏盖掉。
    """
    best, best_ts = None, ""
    for p in (ALT_STATE, ALT_STATE_FALLBACK):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f) or {}
        except Exception:
            continue
        ts = str(d.get("ts") or "")
        if d.get("last") and ts >= best_ts:
            best, best_ts = d.get("last"), ts
    return best


def alt_next(seq, last=None):
    """按「上一次打的」推出下一次该打哪个；没记录就从序列第一个开始"""
    last = last or alt_last()
    if last in seq:
        return seq[(seq.index(last) + 1) % len(seq)]
    return seq[0]


def alt_save(name):
    """记下这次真打的类型 → 下一局自动切到序列里的下一个

    主路径写失败就退到 %TEMP%/KKAuto/secret_alt.json（只读安装目录的兜底）。
    """
    payload = {"last": name, "cn": ALT_CN.get(name, name),
               "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    errs = []
    for p in (ALT_STATE, ALT_STATE_FALLBACK):
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if p != ALT_STATE:
                print(f"   ℹ 交替状态改存兜底路径：{p}", flush=True)
            return True
        except Exception as e:
            errs.append(f"{p}: {e}")
    print(f"   ⚠ 写 secret_alt.json 失败（主路径+兜底都不行）：{'; '.join(errs)}", flush=True)
    return False


def resolve_difficulty(root, args):
    """判断本局难度，返回 (名称, 数字, 是否低难度 N1~N7, 来源)。

    判定顺序：
      1) 命令行 --difficulty N3
      2) full_loop.json 的 difficulty.name（将来软件界面上那个输入框）
      3) 大厅选难度时落盘的 logs/lobby_difficulty.json（按点的那一行推 N<行号>）

    行数推导只在「难度列表 N1..Nk 连续」时成立；识别不到 / 推不出来就返回
    (None, None, None, ...)，调用方按「不改变现有行为」处理，不会误跳过秘境。
    """
    d = root.get("difficulty", {}) or {}
    low_max = int(d.get("low_max", 7))

    name = (getattr(args, "difficulty", "") or d.get("name") or "").strip()
    src = "配置"
    if not name:
        j = read_diff_rec()
        if not j:
            return None, None, None, "未知"
        name = str(j.get("text_guess") or f"N{j.get('rows')}")
        src = f"大厅行数自动({j.get('rows')} 行)"

    m = re.match(r"[Nn]?\s*(\d+)", name)
    num = int(m.group(1)) if m else None
    is_low = (num is not None and num <= low_max)
    return name, num, is_low, src



# ---------------------------------------------------------------- 基础工具
def sh(cmd, label, timeout=1200):
    """跑子进程，回显末尾 12 行，返回 (exit, 输出)

    2026-11-17：改成 Popen + 轮询，这样「检测到登录窗口」时能立刻掐掉正在跑的子进程，
    而不是傻等到它跑完（一个阶段最长好几分钟，期间它会一直点）。
    """
    t0 = time.time()
    print(f"\n▶ {label}", flush=True)
    if LOGIN["ts"]:
        return RC_LOGIN, ""
    # 用户 2026-11-17 17:31：退出游戏后的活动弹窗（首充礼包 / 我要变强）要「第一时间优先处理」，
    # 不能等下一轮才开始点 —— 它会盖住平台、吃掉所有点击。这里每段开跑前先扫一遍。
    try:
        close_popups("🪟 开跑前弹窗检查")
    except Exception:
        pass
    # 单局看门狗：剩余时间不够就别再启动新步骤了，也别让一个阶段跑过 50 分钟太多。
    # 超出预算返回 1 + [ROUND_TIMEOUT] 标记，由调用方按「卡死」处理（杀 War3 → 重开游戏 → 重跑本轮，不重启平台）。
    if ROUND["deadline"]:
        left = ROUND["deadline"] - time.time()
        if left <= 0:
            ROUND_HIT["v"] = True
            print(f"   ⏱ 单局已超过 {ROUND['limit']} 分钟 → 不再启动「{label}」", flush=True)
            return 1, "[ROUND_TIMEOUT]"
        cap = int(left) + 20
        if timeout > cap:
            print(f"   ℹ️ 单局只剩 {int(left)}s → 「{label}」超时从 {timeout}s 收到 {cap}s", flush=True)
            timeout = cap
    try:
        p = subprocess.Popen([PY, "-W", "ignore"] + cmd, cwd=HERE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"   ❌ 启动失败 {e!r}", flush=True)
        return 1, ""
    SH_PROC["p"] = p
    out = ""
    rc = 1
    # 边跑边打：2026-11-17 17:05 实测「① 开局」106 秒里界面只显示一行，
    # 原因是原来用 communicate(timeout=0.5) 轮询，输出要等子进程退出才拿到。
    # 改成后台线程逐行读 + 立刻回显，`out` 仍然完整保留给后面的标记判断。
    lines: list[str] = []

    def _pump():
        try:
            for ln in p.stdout:                      # type: ignore[union-attr]
                ln = ln.rstrip("\r\n")
                lines.append(ln)
                if ln.strip():
                    print("   " + ln, flush=True)
        except Exception:
            pass

    th = threading.Thread(target=_pump, daemon=True)
    th.start()
    try:
        while True:
            if LOGIN["ts"]:                      # 登录窗出现 → 立刻住手
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True)
                except Exception:
                    pass
                print("   ⛔ 检测到登录窗口 → 掐断当前子进程", flush=True)
                rc = RC_LOGIN
                break
            if time.time() - t0 > timeout:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True)
                except Exception:
                    pass
                print(f"   ⏱ {label} 超时 {timeout}s 被终止", flush=True)
                if ROUND["deadline"] and time.time() >= ROUND["deadline"] - 1:
                    ROUND_HIT["v"] = True
                    print(f"   ⏱ 这次超时是因为单局已超过 {ROUND['limit']} 分钟 → 按卡死处理",
                          flush=True)
                rc = 1
                break
            try:
                rc = p.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        SH_PROC["p"] = None
        try:
            th.join(timeout=2)
        except Exception:
            pass
        try:
            if p.stdout:
                p.stdout.close()
        except Exception:
            pass
    out = "\n".join(lines)
    if any(t in out for t in BACK_TOKENS):
        BACK_HIT["v"] = True
    print(f"   ⏱ {label} 用时 {time.time() - t0:.1f}s (exit={rc})", flush=True)
    return rc, out


def find_war3():
    import win32gui

    hits = []

    def cb(h, _):
        if win32gui.GetWindowText(h).lower().startswith("warcraft"):
            hits.append(h)

    win32gui.EnumWindows(cb, None)
    for h in hits:
        if win32gui.IsIconic(h):
            win32gui.ShowWindow(h, 9)
    return hits


def load_cfg(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 各阶段
def park_mouse(verbose=True):
    """鼠标别停在屏幕四个角上。

    踩过的坑：点「返回平台」失败时把光标 SetCursorPos 到了 (0,0)，
    下一轮 pipeline 里 pydirectinput 直接把「鼠标在角落」当成紧急停止（FAILSAFE），
    开局任务 6 秒就抛 FailSafeException。这里每轮开始前兜一下。
    """
    try:
        import win32api
        x, y = win32api.GetCursorPos()
        w, h = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
        if x < 10 or y < 10 or x > w - 10 or y > h - 10:
            win32api.SetCursorPos((w // 2, h // 2))
            if verbose:
                print("   🖱 鼠标原在屏幕角落（会触发 pydirectinput 紧急停止）→ 挪到屏幕中间",
                      flush=True)
    except Exception:
        pass


def close_popups(label="🪟 随机弹窗检查"):
    """看到平台活动弹窗（首充礼包之类）就点它的 X 关掉。

    2026-11-27 00:10 用户要求：**恢复广告弹窗的「点击」关闭**（之前 11-26 停用过）。
    用户 2026-11-16 22:42：退出游戏后又会随机冒出促销弹窗，「看到直接点 X 关闭就可以了，
    随机弹窗事件，记录为随机弹窗方便归类」。这里静默调用 popup_guard.py：
    - 真有弹窗才会打印（🪟 随机弹窗：首充礼包 → 点 X…）
    - 每次都记一条 logs/popup_events.jsonl（kind=随机弹窗），方便事后统计有几次
    调用失败绝不影响主流程。
    """
    try:
        p = subprocess.run([PY, "-W", "ignore", "popup_guard.py", "--close", "--quiet"],
                           cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90)
        out = (p.stdout or "").strip()
        if out:
            print(f"   {label}", flush=True)
            for ln in out.splitlines():
                if ln.strip():
                    print("   " + ln.strip(), flush=True)
    except Exception as e:
        print(f"   (随机弹窗检查跳过：{e!r})", flush=True)


def start_popup_watch():
    """拉起「弹窗常驻监控」：不关闭、一直盯，出现就点（用户 2026-11-17 21:50 要求）

    2026-11-27 00:10 用户要求：恢复广告弹窗的「点击」关闭（之前 11-26 停用过）。

    为什么不是「某个时机扫一次」：实测退出游戏后红包弹窗**约 3 秒**才出现
    （21:26:12 还没有 / 21:26:15 命中），任何一次性扫描都可能正好打空。
    常驻进程 0.8 秒看一次，出现即处理，没有空窗。
    单实例由 popup_watch.py 里的命名互斥体保证，重复调用无副作用。
    """
    try:
        st = subprocess.run([PY, os.path.join(HERE, "popup_watch.py"), "--status"],
                            cwd=HERE, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30,
                            creationflags=0x08000000)
        if "在跑" in (st.stdout or ""):
            return
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        subprocess.Popen([PY, os.path.join(HERE, "popup_watch.py"),
                          "--minutes", "300", "--interval", "0.8"],
                         cwd=HERE, env=env,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x00000008)     # DETACHED_PROCESS
        print("   🛰 弹窗常驻监控已启动（出现即处理，整轮有效）", flush=True)
    except Exception as e:
        print(f"   (弹窗常驻监控启动失败：{e!r})", flush=True)



def wait_war3_gone(seconds=30, step=1.0):
    """退出游戏的**操作全部执行完毕**后再等最多 N 秒，让 War3 进程自己退干净。

    2026-11-20 01:55 用户定：退出操作做完、进程还在 → 等 30 秒；30 秒后仍在 → 结束进程。
    （有些机器最后一步确认后要几十秒才退干净，立刻判失败会白补退/误杀。）
    进程消失立刻返回 True；等满还在返回 False。
    """
    t0 = time.time()
    while time.time() - t0 < seconds:
        if not find_war3():
            return True
        time.sleep(step)
    return not find_war3()


def ensure_war3_closed(after="退出操作", seconds=30):
    """统一收尾：退出操作完毕 → 等 N 秒（进程消失立即返回）→ 仍在 → 结束进程。

    2026-11-20 01:55 用户定。返回 True = 已经没有 War3 进程。
    """
    if not find_war3():
        return True
    print(f"   ⏳ {after}完毕，进程还在 → 等最多 {seconds} 秒让它自己退干净", flush=True)
    if wait_war3_gone(seconds):
        print("   ✅ War3 已自行退出", flush=True)
        return True
    print(f"   ⛔ 等满 {seconds} 秒进程仍在 → 结束进程", flush=True)
    try:
        import cheat_guard as cg
        cg.kill_war3()
    except Exception as e:
        print(f"   (杀进程失败：{e})", flush=True)
    time.sleep(2.0)
    return not find_war3()


def stage_enter(cfg, rec, args):
    """A 进场：清场 → 房间校验/复用 → pipeline → 等进游戏"""
    import loop_round as lr

    # 上一轮退出游戏后平台可能弹促销窗（首充礼包 / 我要变强 / 216U币红包），
    # 实测是**退出后约 3 秒**才冒出来 → 先拉起常驻监控（没有空窗），再顺手扫一次
    start_popup_watch()
    close_popups()

    # A0 清场
    if cfg.get("cleanup", True) and find_war3():
        print("   ⚠️ 上轮 War3 没退干净 → 先退出", flush=True)
        _quit_args = ["game_auto.py", "run", "--config", "configs/war3_exit.json",
                      "--task", "quit_game", "--autostart"]
        sh(_quit_args, "⓪ 清场：退出残留 War3", timeout=180)
        # 2026-11-21 01:26 用户定：退出只保留 **两种方式** ——
        #   ① F10 → E → Q → O（上面 sh() 的模拟按键正常退出）
        #   ② 结束进程（等 30 秒还没退 → kill_war3 强杀）
        # 不再做「补退 1 次」；正常情况（游戏自己退出）零额外耗时。
        if not wait_war3_gone(30):
            # 2026-11-21 01:26 用户定：退出只保留两种方式 ——
            # ① F10 → E → Q → O（正常退出）；② 结束进程（强杀）。不再做「补退 1 次」。
            print("   ⛔ 等满 30 秒进程仍在 → 结束进程", flush=True)
            try:
                import cheat_guard as cg
                cg.kill_war3()
            except Exception as e:
                print(f"   (杀进程失败：{e})", flush=True)
            time.sleep(2.0)
        if find_war3():
            rec["A 进场"] = "失败(War3 无法结束)"
            return False

    # A1 房间现状（pipeline 内部会再做一次严格校验）
    room = None
    try:
        import room_finder as rf
        import game_auto as ga
        room = ga._win7_room_pair(rf.platform_windows(min_client=300))
        if room is None:
            room = rf.find_room(verbose=False)
    except Exception as e:
        print(f"   (房间探测失败，按无房间处理：{e})", flush=True)
    if room:
        print(f"   ℹ️ 已有房间 hwnd={room['hwnd']} "
              f"({room['client'][2]}x{room['client'][3]}) → 开局会跳过搜图/建房/填密码", flush=True)
        rec["房间"] = "复用已有"
    else:
        rec["房间"] = "新建"

    # A2 开局：一次「尝试」= 从第 1 步搜索地图 → 进详情页 → 建房（整条 pipeline）。
    # 用户 2026-11-17 15:07：无法创建房间就按这个口径试 4 次、每次间隔 6 分钟，
    # 4 次都不行才重启平台（一次运行/每轮最多自动重启一次）。
    try:
        max_busy = max(1, int(cfg.get("busy_attempts", 4) or 4))
    except Exception:
        max_busy = 4
    try:
        busy_min = max(0.0, float(cfg.get("busy_wait_minutes", 0) or 0))
    except Exception:
        busy_min = 0.0
    # 超时预算：pipeline 5 步约 60~90s/次 + 每次失败后的等待（最后一次不再等）
    # 基础值 600 → 900（17:52）→ 1200（20:33）：单次尝试里最长的等待是 ⑤ 大厅等「开始游戏」
    # 按钮（configs/wuxia_lobby.json，用户 20:33 要求放宽到 15 分钟 = 900s），再加 ①~④ 约 100~150s，
    # 一次尝试最坏 ~1050s。基础预算留小了，「无法创建房间尝试次数」设成 1 时外层会先掐掉子进程，
    # ⑤ 自己的超时永远等不到，日志只会显示「开局超时」——查不到真因。所以 ≥ 900+300。
    budget = 1200 + int((busy_min * 60 + 120) * max(0, max_busy - 1))
    print(f"   ℹ️ 开局超时 {budget}s（最多 {max_busy} 次尝试"
          + (f"，每次失败等 {busy_min:g} 分钟" if busy_min > 0 else "")
          + "）", flush=True)

    # ---- 开局重试策略（用户 2026-11-17 14:45，15:03 / 15:07 两次修订）----
    # 「无法创建房间」= 僵尸会话（13）或建房子弹窗不出来（11）→ 整段从第 1 步重试，
    # 共 max_busy 次；全都不行才重启平台一次。
    # 「无法正常开始游戏」（点了开始没进地图 / 其它 exit≠0）用户 15:03 删除，直接判本轮失败。
    attempt = 0
    used_platform = 0
    max_platform = 1 if cfg.get("restart_on_enter_fail", True) else 0
    while True:
        attempt += 1
        # 最后一次不再等：等完也是要重启平台，白等 6 分钟没意义
        bw_this = busy_min if attempt < max_busy else 0
        label = ("① 开局（pipeline 5 步）" if attempt == 1
                 else f"① 开局（第 {attempt}/{max_busy} 次尝试：从搜图重来）")
        rc, _ = sh(build_cmd("enter", cfg, args, busy_wait=bw_this),
                   label, timeout=budget)

        if rc == 0:
            # ---- A3 等进地图：进不去就是本轮失败，不重启平台（用户 15:03 删除该条件）----
            if lr.wait_ingame(cfg.get("timeout_in_game", 150), "②"):
                rec["A 进场"] = "成功"
                return True
            rec["A 进场"] = "失败(点了开始但没进地图)"
            return False
        if rc == RC_RESTARTED:
            used_platform += 1
            if used_platform > 1:
                rec["A 进场"] = "失败(KK 重启后仍重复切换/游戏中)"
                print("   ⛔ KK 已重启一次仍卡住，停止继续重启", flush=True)
                return False
            rec["重启平台"] = "重复窗口或房间卡在游戏中"
            print("   ♻️ KK 已重启 → 从搜索地图重新开始本轮", flush=True)
            attempt = 0
            continue
        if rc in (RC_LOGIN, RC_DISC):
            break
        if rc in (RC_BUSY_RETRY, RC_NODLG):
            why = ("无法创建房间（僵尸会话没释放）" if rc == RC_BUSY_RETRY
                   else "无法创建房间（建房子弹窗不出来 / 退房失败 / 详情页丢失）")
            if (attempt < max_busy and not RISK_HIT["v"]
                    and not LOGIN["ts"] and not ROUND_HIT["v"]):
                print(f"   ♻️ 第 {attempt}/{max_busy} 次尝试没成（{why}）"
                      f" → 从第 1 步（搜索地图）重新来", flush=True)
                continue
            why = f"无法创建房间（{max_busy} 次尝试都没成功）"
        elif rc == RC_BUSY:
            BUSY_HIT["v"] = True
            rec["A 进场"] = "失败(僵尸会话等待被打断)"
            return False
        else:
            # 其它失败（含「无法正常开始游戏」）不再重启平台
            rec["A 进场"] = f"失败(开局 exit={rc})"
            return False

        # 4 次都建不出房间 → 重启平台一次，重开后重新从第 1 步来（尝试次数重新计）
        if (used_platform < max_platform and not RISK_HIT["v"]
                and not LOGIN["ts"] and not ROUND_HIT["v"]):
            used_platform += 1
            ok, st = restart_platform(why)
            if ok:
                rec["重启平台"] = why
                print(f"   ↻ 平台已重开 → 重新从第 1 步来（{max_busy} 次机会重新算）", flush=True)
                attempt = 0
                continue
            if st == "login":
                LOGIN["ts"] = LOGIN["ts"] or time.strftime("%Y-%m-%d %H:%M:%S")
                rc = RC_LOGIN
                break
            rec["A 进场"] = f"失败({why} → 重启平台失败：{st})"
            return False
        rec["A 进场"] = f"失败({why})"
        BUSY_HIT["v"] = True
        return False

    if rc == RC_LOGIN:
        LOGIN["ts"] = LOGIN["ts"] or time.strftime("%Y-%m-%d %H:%M:%S")
        rec["A 进场"] = "中止(检测到登录窗口)"
        return False
    if rc == RC_BUSY:
        BUSY_HIT["v"] = True
        rec["A 进场"] = "失败(僵尸会话未释放，需重启平台)"
        return False
    rec["A 进场"] = f"失败(开局 exit={rc})"
    return False


def stage_prepare(cfg, rec, args):
    """B 局内准备四步"""
    rc, out = sh(build_cmd("prepare", cfg, args),
                 "③ 局内四步（技能/装备/升级/自动挑战）", timeout=420)
    # in_game 检测到「官方检测该账号有作弊风险」时会杀 War3 并 exit=3。
    # 原文：「如果再次出现则永久封禁」→ 立刻停整轮，绝不能再自动开下一局。
    if rc == 3 or "作弊" in (out or ""):
        RISK_HIT["v"] = True
        rec["B 准备"] = "中止(风控弹窗：作弊风险)"
        return False
    # in_game 成功时末尾会打「全部完成」之类，统一按 exit 判
    rec["B 准备"] = "成功" if rc == 0 else f"失败(exit={rc})"
    return rc == 0


def stage_clear(cfg, rec, args):
    """C 提前挑战 + 挑战存档：watch_early 一体完成（不做 12 格存档挑战）"""
    # 2026-11-19 20:00 用户定：④ 段登录守卫由 watch_early 进程内承担，
    # 主进程这份暂停，出 ④（无论成败）恢复 —— 全程无守卫空窗、无双监控并存。
    login_watch_stop()
    try:
        rc, out = sh(build_cmd("clear", cfg, args),
                     "④ 提前挑战 + 挑战存档",
                     timeout=int(cfg.get("timeout", 1200)) + 300)
    finally:
        login_watch_start()
    if rc == RC_LOGIN:
        # watch_early 里的守卫命中 → 它自己 exit 131 停手；这里接住并按登录命中走
        LOGIN["ts"] = LOGIN["ts"] or time.strftime("%Y-%m-%d %H:%M:%S")
        rec["C 提前挑战"] = "中止(检测到登录窗口)"
        return False
    if rc == 0:
        rec["C 提前挑战"] = "成功"
        return True
    if rc == 2:
        # 2026-11-21 01:06 用户要求：从「点开始游戏」到「挑战存档」超过 30 分钟还没出现
        # → 直接退出游戏（不干等，也不再往下走 D/E）
        rec["C 提前挑战"] = "超时(未见挑战存档) → 已退出游戏"
        print("   ⛔ 30 分钟没等到「挑战存档」→ 退出游戏（本轮作废，下一轮重开）", flush=True)
        if cfg.get("quit", True):
            # 2026-11-21 01:20 修：必须先走「模拟按键正常退出」（F10→E→X→X），
            # 直接 ensure_war3_closed 会等到超时后强杀 → 平台判异常掉线 → 房间锁「游戏中」。
            sh(["game_auto.py", "run", "--config", "configs/war3_exit.json",
                "--task", "quit_game", "--autostart"], "⓪ 退出游戏（C 超时）", timeout=180)
            ensure_war3_closed("C 超时退出", 30)
        return False
    rec["C 提前挑战"] = f"失败(exit={rc})"
    return False


def stage_slots(cfg, rec, args):
    """D 存档挑战：面板已弹出 → 12 格双击 x N 轮（必须在秘境之前）"""
    rc, out = sh(build_cmd("slots", cfg, args), "⑤ 存档挑战（12 格双击）", timeout=300)
    if rc == 0:
        rec["D 存档挑战"] = f"成功({cfg.get('rounds', 2)} 轮)"
        return True
    if rc == 4:
        rec["D 存档挑战"] = "跳过(面板未开)"
        return cfg.get("require_panel", True) is False
    rec["D 存档挑战"] = f"失败(exit={rc})"
    return False


def stage_secret(cfg, rec, args):
    """E 秘境挑战：勾了哪个打哪个"""
    island = bool(cfg.get("island", False))
    undead = bool(cfg.get("undead", False))
    delta = bool(cfg.get("delta", False))
    if not island and not undead and not delta:
        print("   （海岛、不死、三角洲都没勾 → 跳过秘境挑战）", flush=True)
        rec["E 秘境"] = "跳过(未勾选)"
        return True

    picks = [n for n, v in (("海岛", island), ("不死", undead), ("三角洲", delta)) if v]
    print(f"   勾选：{' + '.join(picks)}（确认方式："
          f"{'点「是」真打' if cfg.get('confirm', 'yes') == 'yes' else '点「否」演练'}）", flush=True)

    rc, out = sh(build_cmd("secret", cfg, args),
                 f"⑥ 秘境挑战（{' + '.join(picks)}）", timeout=420)
    rec["E 秘境"] = ("成功: " + " + ".join(picks)) if rc == 0 else f"失败(exit={rc})"
    return rc == 0


def gui_settle_timeout():
    """读成品软件里设置的「等清算最多等几分钟」→ 秒（2026-11-20 16:34 新增）。

    GUI 存在 %APPDATA%/KKAuto/gui_settings.json 的 settle_min 字段；没设置返回 None。
    """
    try:
        import json as _json
        p = os.path.join(os.environ.get("APPDATA", ""), "KKAuto", "gui_settings.json")
        with open(p, encoding="utf-8") as f:
            v = _json.load(f).get("settle_min")
        if v is not None:
            return int(float(v) * 60)
    except Exception:
        pass
    return None


def stage_finish(cfg, rec, args):
    """F 胜利收尾：等结算 → 继续观战 → 退出游戏"""
    # 2026-11-20 16:34 新增：等清算时间 = 软件里设的（默认 5 分钟）> 配置 > 默认
    settle_s = gui_settle_timeout() or int(cfg.get("settle_timeout", cfg.get("timeout", 1500)))
    if settle_s != int(cfg.get("settle_timeout", -1)):
        print(f"   ⏳ 等清算时长取软件设置：{settle_s}s（{settle_s // 60} 分钟）", flush=True)
        cfg = dict(cfg)
        cfg["settle_timeout"] = settle_s
    rc, out = sh(build_cmd("finish", cfg, args),
                 "⑦ 收尾（等结算 → 继续观战 → 退出游戏）",
                 timeout=settle_s + 300)
    if rc == 0:
        # 2026-11-20 01:55 用户定：退出操作完毕、进程还在 → 等 30 秒 → 仍在则结束进程
        if cfg.get("quit", True):
            ensure_war3_closed("收尾退出游戏操作", 30)
        rec["F 收尾"] = "成功" if cfg.get("quit", True) else "成功(未退出)"
        return True
    if rc == 4:
        # 结算界面是「失败」大字（秘境挑战 / 主游戏这场没打过）。
        # 用户 2026-11-18 09:49：遇见失败直接点「继续观战」再退出游戏。
        # 注意：点「继续观战」只是关掉失败面板，主游戏其实还在继续（实测 13/25 波仍在跑），
        # 所以这里退出等于放弃剩下的波次 —— 但本局的通关/存档收益在 C、D 阶段已经拿到。
        rec["F 收尾"] = "成功(遭遇失败→退出)"
        if rec.get("E 秘境"):
            rec["E 秘境"] = f"{rec['E 秘境']}→战斗失败"
        return True
    if rc == 2:
        # 2026-11-20 16:34 用户要求：等清算超时（既没「胜利」也没「失败」）→ 自动退出游戏，不干等
        print(f"   ⏳ 等清算超时（{settle_s}s 内没出现胜利/失败）→ 自动退出游戏", flush=True)
        rec["F 收尾"] = f"超时(等清算 {settle_s}s 没结果) → 已自动退出游戏"
        if cfg.get("quit", True):
            ensure_war3_closed("等清算超时后退出游戏", 30)
        return False
    rec["F 收尾"] = {1: "失败(结算点不动)", 2: "超时(没等到结算)",
                     3: "失败(游戏不在)"}.get(rc, f"失败(exit={rc})")
    return False


STAGE_FN = {"enter": stage_enter, "prepare": stage_prepare,
            "clear": stage_clear, "slots": stage_slots,
            "secret": stage_secret, "finish": stage_finish}


def stage_quit_direct(cfg, rec, args):
    """低难度（N1~N7）不打秘境时的收尾：存档挑战打完等够时间 → 直接退游戏。

    用户 2026-11-16 16:53 / 21:09：N1~N7 默认不打秘境挑战，打完存档挑战等 60 秒就能退出循环。
    所以不走 watch_victory（那个要等「胜利」结算，低难度下没必要）。
    """
    rc, _ = sh(["game_auto.py", "run", "--config", "configs/war3_exit.json",
                "--task", "quit_game", "--autostart"],
               "⑦ 收尾（低难度：直接退出游戏）", timeout=180)
    if rc == 0:
        # 2026-11-20 01:55 用户定：退出操作完毕、进程还在 → 等 30 秒 → 仍在则结束进程
        ensure_war3_closed("直接退出游戏操作", 30)
    rec["F 收尾"] = "成功(直接退出)" if rc == 0 else f"失败(exit={rc})"
    return rc == 0



def build_cmd(k, cfg, args, busy_wait=None):
    """把某阶段要执行的命令拼出来（dry 预览与实际执行共用，避免两套说法）

    busy_wait：临时覆盖「遇到『您正在游戏中』等几分钟」（最后一次尝试传 0，免得白等）
    """
    if k == "enter":
        cmd = ["game_auto.py", "pipeline", "--pipeline", "configs/pipeline.json"]
        # 僵尸会话（强杀 War3 后「您正在游戏中，不能创建游戏房间」）等多久从配置透传，
        # 这样改 full_loop.json 一处就同时管住 pipeline 与超时预算。
        # 重试次数不再透传：整段尝试次数由 full_loop.stage_enter 数（用户 2026-11-17 15:07）。
        bw = cfg.get("busy_wait_minutes") if busy_wait is None else busy_wait
        if bw is not None:
            cmd += ["--busy-wait", str(bw)]
        return cmd
    if k == "prepare":
        cmd = ["in_game.py", "--skill", str(cfg.get("skill", 3)),
               "--equip", str(cfg.get("equip", 4)),
               "--upgrade", str(cfg.get("upgrade", 3))]
        if args.skip_upgrade:
            cmd += ["--skip", "3"]
        return cmd
    if k == "clear":
        return ["watch_early.py", "--stop-after-save", "--no-slots",
                "--interval", str(cfg.get("interval", 60)),
                "--timeout", str(cfg.get("timeout", 1200))]
    if k == "slots":
        cmd = ["save_challenge.py", "--rounds", str(cfg.get("rounds", 2)),
               "--gap", str(cfg.get("gap", 1.0))]
        if cfg.get("require_panel", True):
            cmd.append("--require-panel")
        return cmd
        return cmd
    if k == "secret":
        cmd = ["secret_challenge.py"]
        if cfg.get("island"):
            cmd.append("--island")
        if cfg.get("undead"):
            cmd.append("--undead")
        if cfg.get("delta"):
            cmd.append("--delta")
        if cfg.get("confirm", "yes") == "no":
            cmd.append("--no")
        return cmd
    if k == "finish":
        # 2026-11-20 16:34 用户要求（基于 v1.42 新增）：等清算时间可自定义（软件里设，默认 5 分钟）
        cmd = ["watch_victory.py", "--interval", str(cfg.get("interval", 3)),
               "--timeout", str(cfg.get("settle_timeout", cfg.get("timeout", 1500)))]
        # 遇到「失败」结算怎么办：quit=点继续观战后退出（默认，用户 2026-11-18 要求）；
        # wait=只关面板，继续等主游戏的胜利结算。
        if cfg.get("on_fail"):
            cmd += ["--on-fail", str(cfg["on_fail"])]
        if not cfg.get("quit", True):
            cmd.append("--no-quit")
        return cmd
    return []


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(
        description="通关循环：A 进场 → B 准备 → C 提前挑战 → D 存档挑战 → E 秘境 → F 收尾")
    ap.add_argument("--config", default=DEF_CFG)
    ap.add_argument("--rounds", type=int, default=None,
                    help="轮数；0 = 无限循环一直打下去（不给则取配置里的 rounds）")
    ap.add_argument("--only", default="", help="只跑这些阶段，如 A,B 或 enter,prepare")
    ap.add_argument("--skip", default="", help="跳过这些阶段，如 D,E")
    ap.add_argument("--island", action="store_true", help="勾上海岛挑战")
    ap.add_argument("--no-island", action="store_true", help="取消海岛挑战")
    ap.add_argument("--undead", action="store_true", help="勾上不死挑战")
    ap.add_argument("--no-undead", action="store_true", help="取消不死挑战")
    ap.add_argument("--delta", action="store_true", help="勾上三角洲挑战（潮汐监狱，2026-11-27 新增）")
    ap.add_argument("--no-delta", action="store_true", help="取消三角洲挑战")
    ap.add_argument("--no-secret", action="store_true", help="整段跳过秘境挑战")
    ap.add_argument("--no-clear", action="store_true", help="跳过提前挑战 + 挑战存档")
    ap.add_argument("--no-slots", action="store_true", help="跳过存档挑战（12 格双击）")
    ap.add_argument("--no-quit", action="store_true", help="收尾时不退出游戏（留着现场）")
    ap.add_argument("--skip-upgrade", action="store_true", help="四步跳过升级武器（省金币）")
    ap.add_argument("--difficulty", default="", metavar="N3",
                    help="指定本局难度（如 N3 / 6）；留空则读配置或大厅识别结果")
    ap.add_argument("--difficulty-seq", default="", metavar="N1,N2,N3",
                    help="逐轮难度序列（如 7 局选 N1~N7）：--difficulty-seq N1,N2,N3,N4,N5,N6,N7")
    ap.add_argument("--secret-alt", default="", metavar="海岛,不死",
                    help="秘境类型交替序列（默认取 stages.secret.alt）；如 --secret-alt 海岛,不死")
    ap.add_argument("--no-secret-alt", action="store_true",
                    help="关掉秘境交替，按配置里勾选的类型打")
    ap.add_argument("--no-low-rule", action="store_true",
                    help="禁用「N1~N7 不打秘境」规则（照常打秘境）")
    ap.add_argument("--cooldown", type=int, default=-1, metavar="SEC",
                    help="局间冷却秒数（默认取配置 cooldown_between_rounds，0=不冷却）")
    ap.add_argument("--risk-retry", type=int, default=-1, metavar="N",
                    help="遇到风控时「杀 War3 + 重开同一轮」的次数"
                         "（默认取配置 risk_retry；0=不重开；负数=不限次数一直重开）")
    ap.add_argument("--risk-wait", type=int, default=-1, metavar="SEC",
                    help="杀掉 War3 后等多久再重开（默认取配置 risk_wait）")
    ap.add_argument("--max-round-min", type=int, default=-1, metavar="MIN",
                    help="单局最长多少分钟：超过就算卡死 → 杀 War3 + 重开游戏 + 重跑本轮"
                         "（默认取配置 max_round_minutes，50 分钟；0=不限制）")
    ap.add_argument("--busy-attempts", type=int, default=-1, metavar="N",
                    help="「无法创建房间」时整段（搜图→建房）试几次才重启平台"
                         "（默认取配置 busy_attempts=4）")
    ap.add_argument("--busy-wait", type=int, default=-1, metavar="MIN",
                    help="卡「您正在游戏中」时每次等几分钟（默认取配置 busy_wait_minutes=6）")
    ap.add_argument("--no-busy-restart", action="store_true",
                    help="试满都不行也不重启平台（界面取消勾选「试满都不行就自动重启平台」）")
    ap.add_argument("--dry", action="store_true", help="只打印计划")
    args = ap.parse_args()

    root = load_cfg(args.config)
    st = root.get("stages", {})

    # 界面上的「正在游戏中等待 N 分钟 / 试几次」通过环境变量透传（GUI → engine → 这里 → pipeline）
    for env_k, cfg_k, cast in (("KK_BUSY_WAIT_MIN", "busy_wait_minutes", float),
                               ("KK_BUSY_ATTEMPTS", "busy_attempts", int),
                               ("KK_RESTART_ON_ENTER_FAIL", "restart_on_enter_fail",
                                lambda v: str(v).strip() not in ("0", "", "false", "False")),
                               ("KK_MAX_ROUND_MIN", "max_round_minutes", int)):
        v = os.environ.get(env_k, "").strip()
        if v:
            try:
                root[cfg_k] = cast(v)
                print(f"   取自界面（环境变量 {env_k}）：{cfg_k} = {root[cfg_k]}", flush=True)
            except Exception:
                pass

    # 单局看门狗：一局超过 N 分钟还没打完 = 卡死 → 杀 War3 + 重开游戏 + 重跑本轮（不重启平台）。
    # 用户 2026-11-17 15:20：平台重启只留给「无法创建房间」这一条。
    ROUND["limit"] = (int(args.max_round_min) if args.max_round_min >= 0
                      else int(root.get("max_round_minutes", 50)))
    # 无法创建房间时「搜图→建房」整段试几次（用户 2026-11-17 15:07：4 次，每次间隔 6 分钟）
    if args.busy_attempts > 0:
        root["busy_attempts"] = int(args.busy_attempts)
    # 界面那两项（等待分钟 / 是否重启平台）也允许走命令行，避免只依赖环境变量继承
    if args.busy_wait >= 0:
        root["busy_wait_minutes"] = int(args.busy_wait)
    if args.no_busy_restart:
        root["restart_on_enter_fail"] = False

    # --- CLI 覆盖（等价于界面打钩） ---
    if args.island:
        st.setdefault("secret", {})["island"] = True
    if args.no_island:
        st.setdefault("secret", {})["island"] = False
    if args.undead:
        st.setdefault("secret", {})["undead"] = True
    if args.no_undead:
        st.setdefault("secret", {})["undead"] = False
    if args.delta:
        st.setdefault("secret", {})["delta"] = True
    if args.no_delta:
        st.setdefault("secret", {})["delta"] = False
    if args.no_secret:
        st.setdefault("secret", {})["enabled"] = False
    if args.no_clear:
        st.setdefault("clear", {})["enabled"] = False
    if args.no_slots:
        st.setdefault("slots", {})["enabled"] = False
    if args.no_quit:
        st.setdefault("finish", {})["quit"] = False

    # --- 秘境交替：海岛 → 不死 → 海岛 → …（只在 N8+ 真打秘境时推进） ---
    # 优先级：命令行显式勾选 > --secret-alt > stages.secret.alt > 配置里写死的 island/undead
    secret_explicit = bool(args.island or args.undead or args.no_island or args.no_undead
                           or args.delta or args.no_delta)
    if args.no_secret_alt or args.no_secret or secret_explicit:
        alt_seq = None
    elif args.secret_alt.strip():
        alt_seq = []
        for s in args.secret_alt.replace("，", ",").split(","):
            k = s.strip().lower()
            if k in ALT_ALIAS and ALT_ALIAS[k] not in alt_seq:
                alt_seq.append(ALT_ALIAS[k])
        alt_seq = alt_seq or None
    else:
        alt_seq = alt_seq_of(root)

    # --- 阶段选择 ---
    only = [s.strip() for s in args.only.replace("，", ",").split(",") if s.strip()]
    skip = [s.strip() for s in args.skip.replace("，", ",").split(",") if s.strip()]
    alias = {"A": "enter", "B": "prepare", "C": "clear",
             "D": "slots", "E": "secret", "F": "finish"}
    only = [alias.get(x.upper(), x) for x in only]
    skip = [alias.get(x.upper(), x) for x in skip]

    # 轮数：--rounds 0（或负数）= 无限循环，一直打下去；不给则取配置
    rounds_in = (args.rounds if args.rounds is not None
                 else int(root.get("rounds", 1)))
    forever = rounds_in <= 0
    rounds = rounds_in if not forever else 10 ** 9      # 无限用一个大上限，靠 Ctrl+C 停
    rounds_for_seq = rounds_in if not forever else 0     # 无限模式不预生成逐轮序列
    cooldown = (int(root.get("cooldown_between_rounds", 0))
                if args.cooldown < 0 else int(args.cooldown))
    # 风控恢复：默认不限次数（-1 → 每轮都能重开），可用配置/参数收窄
    risk_retry = (int(root.get("risk_retry", 99))
                  if args.risk_retry < 0 else int(args.risk_retry))
    # 用户 01:13：风控这条约束「无视跳过」→ risk_retry 为负数时一直重开，不设上限
    risk_unlimited = risk_retry < 0
    risk_wait = (float(root.get("risk_wait", 15))
                 if args.risk_wait < 0 else float(args.risk_wait))
    plan = []
    for k in ORDER:
        c = st.get(k, {})
        on = c.get("enabled", True)
        if only and k not in only:
            on = False
        if k in skip:
            on = False
        # 僵尸会话等待策略写在 root（configs/full_loop.json）里，注入每个阶段，
        # 好让 build_cmd 能把 --busy-wait 透传给 pipeline
        for _k in ("busy_wait_minutes", "busy_attempts", "restart_on_enter_fail"):
            if _k in root and _k not in c:
                c[_k] = root[_k]
        plan.append((k, on, c))

    # --- 难度规则：N1~N7 默认不打秘境，存档完等 60s 直接退出；N8+ 打秘境 ---
    dcfg = root.get("difficulty", {}) or {}
    seq = build_seq(root, args, rounds_for_seq)
    if seq:
        dname, dnum, is_low, dsrc = diff_of(seq[0], "逐轮序列第 1 轮",
                                            int(dcfg.get("low_max", 7)))
    else:
        dname, dnum, is_low, dsrc = resolve_difficulty(root, args)
    low_rule = bool(dcfg.get("enabled", True)) and not args.no_low_rule
    skip_secret_low = low_rule and is_low is True and bool(dcfg.get("skip_secret_on_low", True))
    quit_direct_low = low_rule and is_low is True and bool(dcfg.get("quit_direct_on_low", True))
    pause_after_slots = (int(dcfg.get("pause_after_slots", 60))
                         if (low_rule and is_low is True) else 0)
    # 高难度：D 存档挑战打完 → 等这么久再开 E 秘境挑战
    # 用户 2026-11-17 18:14：取消 30s 等待，D 完直接进 E → 默认 0（配置里改非 0 才等）
    pause_before_secret = max(0, int(dcfg.get("pause_before_secret", 0)))
    # 秘境阶段本身是否启用（关掉的话 D 后面等待就没意义）
    secret_on = any(pk == "secret" and pon for pk, pon, _ in plan)
    # 三角洲（2026-11-27 新增）只在 N12+ 生效；本轮难度 < N12 时跳过它、顺延到下一个类型
    secret_cfg = next((c for pk, pon, c in plan if pk == "secret"), {}) or {}
    delta_min_n = int(secret_cfg.get("delta_min_n", 12) or 12)

    low_label = f"N1~N{int(dcfg.get('low_max', 7))}"
    high_label = f"N{int(dcfg.get('low_max', 7)) + 1}+"

    print("=" * 62)
    head = "无限循环（一直打下去，Ctrl+C 停）" if forever else f"{rounds} 轮"
    print(f"通关循环：{head}" + (f"，局间冷却 {cooldown}s" if cooldown else "，无局间冷却")
          + (f"，风控后杀进程重开（不限次数，等 {risk_wait:.0f}s）" if risk_unlimited
             else f"，风控后杀进程重开（最多 {risk_retry} 次，等 {risk_wait:.0f}s）"
             if risk_retry > 0 else "，风控后不重开"))
    print("平台重启策略：只有「无法创建房间」（整段搜图→建房试 "
          + f"{int(root.get('busy_attempts', 4) or 4)} 次"
          + (f"、每次间隔 {float(root.get('busy_wait_minutes', 0) or 0):g} 分钟"
             if float(root.get('busy_wait_minutes', 0) or 0) > 0 else "")
          + "，都不行才重启平台）")
    print("单局看门狗："
          + (f"一轮超 {ROUND['limit']} 分钟还没打完 → 杀 War3 + 重开游戏 + 重跑本轮"
             "（不重启平台，与风控共用重开次数）" if ROUND["limit"] > 0
             else "不限制单局时长"))
    if dname:
        tail = f"  → 低难度 {low_label}：不打秘境" if skip_secret_low else f"  → {high_label}：打秘境"
        print(f"本局难度：{dname}（{dsrc}）{tail}")
    else:
        print(f"本局难度：未知（{dsrc}）→ 按原流程走，不套用低难度规则")
    if seq:
        print(f"逐轮难度：{' → '.join(seq)}   （每轮点大厅第 N 行 = N{parse_num(seq[0])} 起）")
    print("  （每轮 A 阶段选完难度后会再判一次，多轮连跑以当轮实际难度为准）")
    print("-" * 62)
    for k, on, c in plan:
        if k == "secret" and on and skip_secret_low:
            print(f"  [ ] {TAG[k]} {c.get('label', k)}   ← 低难度 {low_label} 默认不打")
            continue
        print(f"  [{ '✔' if on else ' '}] {TAG[k]} {c.get('label', k)}")
        if k == "secret" and on:
            if alt_seq:
                nxt = alt_next(alt_seq)
                chain = " → ".join(ALT_CN[x] for x in alt_seq)
                print(f"        交替：{chain} → …（本次接着打「{ALT_CN[nxt]}」）"
                      f"  确认方式：{c.get('confirm', 'yes')}")
                if "delta" in alt_seq:
                    print(f"        三角洲需 N{delta_min_n}+，低于时自动跳过、顺延到下一个")
            else:
                picks = [n for n, v in (("海岛", c.get("island")), ("不死", c.get("undead")),
                                        ("三角洲", c.get("delta"))) if v]
                print(f"        勾选：{' + '.join(picks) if picks else '（无 → 会跳过）'}"
                      f"  确认方式：{c.get('confirm', 'yes')}")
            if pause_before_secret:
                print(f"        D 存档挑战打完先等 {pause_before_secret}s 再开打（高难度）")
        if k == "slots" and on:
            print(f"        12 格 x {c.get('rounds', 2)} 轮，间隔 {c.get('gap', 1.0)}s"
                  f"  {'' if c.get('require_panel', True) else '（面板没开也继续）'}")
            if pause_after_slots:
                print(f"        打完等 {pause_after_slots}s 再走收尾（低难度规则）")
        if k == "clear" and on:
            print(f"        最长等「挑战存档」{c.get('timeout', 1200)}s，扫描间隔 {c.get('interval', 60)}s")
        if k == "finish" and on and quit_direct_low:
            print("        → 低难度：不等胜利结算，直接退出游戏")
    print("=" * 62, flush=True)

    todo = [k for k, on, _ in plan if on]
    if not todo:
        print("所有阶段都没开 → 什么都不做", flush=True)
        return 0
    if args.dry:
        print("\n将要执行的命令：")
        for k, on, c in plan:
            if on:
                if k == "secret" and skip_secret_low:
                    print(f"  {TAG[k]}  （低难度 {low_label} → 不执行）")
                    continue
                if k == "secret" and alt_seq:
                    sim = alt_last()
                    cur = alt_next(alt_seq, sim)
                    chain = []
                    for _r in range(min(rounds, 8)):     # 无限模式只预览前 8 轮
                        chain.append(ALT_CN[cur])
                        cur = alt_next(alt_seq, cur)
                    print(f"  {TAG[k]}  python secret_challenge.py （交替：{' → '.join(chain)}）")
                    continue
                if k == "finish" and quit_direct_low:
                    print(f"  {TAG[k]}  python game_auto.py run --config configs/war3_exit.json "
                          f"--task quit_game --autostart   （低难度：直接退出）")
                else:
                    print(f"  {TAG[k]}  python " + " ".join(build_cmd(k, c, args)))
        print("\n[dry] 只打印计划，不执行", flush=True)
        return 0

    # 从自动化主界面点开始后，KK 尚未打开时由脚本自动拉起。
    try:
        import plat_restart as pr
        ready = pr.ensure_running(verbose=True)
    except Exception as e:
        print(f"   ⛔ 自动打开 KK 失败：{e!r}", flush=True)
        return 1
    if ready.get("status") == "login":
        print("   ⛔ KK 停在登录页，请登录后重新点开始运行", flush=True)
        return RC_LOGIN
    if ready.get("status") != "ok":
        print(f"   ⛔ KK 未就绪：{ready.get('status')}", flush=True)
        return 1

    all_rec = []
    # 风控后允许「杀进程重开」的次数；不限次数时用一个够大的值（永不耗尽）
    risk_retry_left = 10 ** 9 if risk_unlimited else risk_retry
    retry_streak = 0                      # 连续重开次数（成功进场后清零）
    def rnd(i):
        return f"第 {i} 轮（无限循环）" if forever else f"第 {i}/{rounds} 轮"

    i = 0
    cur_num = dnum          # 本轮实际难度编号（A 阶段点完难度后按大厅记录刷新）
    login_watch_start()
    while i < rounds:
        i += 1
        if check_login(f"第 {i} 轮开始前"):
            return login_abort(f"第 {i} 轮开始前")
        print(f"\n{'#' * 62}\n# {rnd(i)}  {time.strftime('%H:%M:%S')}\n{'#' * 62}", flush=True)
        t0 = time.time()
        rec = {"轮次": i}
        aborted = False
        RISK_HIT["v"] = False
        BACK_HIT["v"] = False
        ROUND_HIT["v"] = False
        # 单局看门狗：本轮从开头算起，超过 limit 分钟还没打完就按「卡死」处理（杀 War3 重开游戏）
        ROUND["deadline"] = (t0 + ROUND["limit"] * 60) if ROUND["limit"] > 0 else None
        if ROUND["deadline"]:
            print(f"   ⏱ 单局上限 {ROUND['limit']} 分钟（到点还没打完 → 杀 War3 重开游戏、重跑本轮）",
                  flush=True)
        park_mouse()
        # 本轮目标难度（逐轮序列优先）→ 写 logs/diff_target.json 给大厅选难度那步读
        this_name = seq[i - 1] if i - 1 < len(seq) else (seq[-1] if seq else "")
        this_num = parse_num(this_name) if this_name else None
        low_max = int(dcfg.get("low_max", 7))
        clear_diff_rec()
        if this_name:
            print(f"🎯 本轮难度 {this_name} → 大厅点第 {this_num} 行", flush=True)
            write_diff_target(this_num, this_name, f"r{i}")
            r_low = low_rule and this_num is not None and this_num <= low_max
            r_skip = r_low and bool(dcfg.get("skip_secret_on_low", True))
            r_quit = r_low and bool(dcfg.get("quit_direct_on_low", True))
            r_pause = int(dcfg.get("pause_after_slots", 60)) if r_low else 0
            r_wait = pause_before_secret if (secret_on and not r_skip) else 0
            rec["目标难度"] = this_name
        else:
            # 自动模式（没指定目标难度）：必须清掉上一轮残留的 logs/diff_target.json。
            # 否则大厅那步还按旧行号点（例：残留 row=8），地图更新后大厅多了 N9（第 9 行）
            # 就永远选不到最高档（用户 2026-11-18：新难度要第一时间能选到）。
            if clear_diff_target():
                print("🧹 已清残留难度目标（自动模式 → 交给大厅选最高档）", flush=True)
            # 本轮的低难度决策：默认沿用启动时的判断，A 阶段选完难度后再刷一次
            r_skip, r_quit, r_pause = skip_secret_low, quit_direct_low, pause_after_slots
            r_wait = pause_before_secret if (secret_on and not r_skip) else 0
        for k, on, c in plan:
            if check_login(f"第 {i} 轮 · {TAG[k]} 阶段前"):
                return login_abort(f"第 {i} 轮 · {TAG[k]} 阶段前")
            if ROUND["deadline"] and time.time() > ROUND["deadline"]:
                ROUND_HIT["v"] = True
                print(f"   ⏱ 单局已超 {ROUND['limit']} 分钟还没打完"
                      f"（卡在 {TAG[k]} 阶段前）→ 本轮中止，杀 War3 重开游戏、重跑本轮", flush=True)
                aborted = True
                break
            if not on:
                if k == "secret" and r_skip:
                    rec["E 秘境"] = f"跳过(低难度 {low_label})"
                continue
            if k == "secret" and r_skip:
                print(f"   ⏭ {TAG[k]} 低难度 {low_label} → 跳过秘境挑战", flush=True)
                rec["E 秘境"] = f"跳过(低难度 {low_label})"
                continue
            r_pick = None            # 本轮秘境实打的类型（island / undead / delta），成功后才推进交替
            delta_ok = (cur_num is not None and cur_num >= delta_min_n)
            if k == "secret":
                # 三角洲（潮汐监狱）只在 N12+ 生效 —— 2026-11-27 用户定。
                # 难度未知也按「不满足」处理：一旦点「是」会全员传送且无法返回，不能赌。
                if c.get("delta") and not delta_ok:
                    c = dict(c)
                    c["delta"] = False
                    print(f"   ⏭ 三角洲挑战要 N{delta_min_n}+（本轮 N{cur_num or '?'}）"
                          f" → 跳过三角洲", flush=True)
                if alt_seq:
                    r_pick = alt_next(alt_seq)
                    if r_pick == "delta" and not delta_ok:
                        for _ in range(len(alt_seq)):
                            r_pick = alt_next(alt_seq, r_pick)
                            if r_pick != "delta":
                                break
                        print(f"   ⏭ 本轮 N{cur_num or '?'} < N{delta_min_n}"
                              f" → 跳过三角洲，改打「{ALT_CN[r_pick]}」", flush=True)
                    c = dict(c)          # 拷一份改，不动配置文件里的勾选
                    c["island"] = (r_pick == "island")
                    c["undead"] = (r_pick == "undead")
                    c["delta"] = (r_pick == "delta")
                    print(f"   🔁 秘境交替：本局打「{ALT_CN[r_pick]}」"
                          f"（上次 {ALT_CN.get(alt_last()) or '无记录'}）", flush=True)
                elif not (c.get("island") or c.get("undead") or c.get("delta")):
                    print("   ⏭ 本轮没有可打的秘境类型 → 跳过 E 阶段", flush=True)
                    rec["E 秘境"] = "跳过(无可打类型)"
                    continue
            try:
                if k == "finish" and r_quit:
                    ok = stage_quit_direct(c, rec, args)
                else:
                    ok = STAGE_FN[k](c, rec, args)
            except Exception as e:
                rec[f"{TAG[k]} {c.get('label', k)}"] = f"异常({e!r})"
                ok = False
            if ok and r_pick:
                alt_save(r_pick)     # 打成功了才推进，失败/中断的话下一局还打同一个
            if not ok:
                if ROUND_HIT["v"]:
                    print(f"   ⏱ 单局超过 {ROUND['limit']} 分钟还没打完（卡在 {TAG[k]} 阶段）"
                          f"→ 本轮中止，杀 War3 重开游戏、重跑本轮", flush=True)
                elif RISK_HIT["v"]:
                    print("   ⛔ 触发「官方检测该账号有作弊风险」→ 本轮中止，"
                          "接下来结束 War3 进程并重开游戏继续", flush=True)
                elif BACK_HIT["v"]:
                    print("   ⛔ 游戏提前结束（点「返回平台」回平台）→ 本轮中止，"
                          "接下来重开游戏、重跑这一轮", flush=True)
                else:
                    print(f"   ⛔ {TAG[k]} 阶段没走通 → 本轮中止（后续阶段可能被动做也做不对）", flush=True)
                aborted = True
                break
            # A 选完难度 → 用当轮「实际点的那一行」重算低难度规则（多轮连跑不会串味）
            if k == "enter":
                j = read_diff_rec()
                # 换账号后大厅只显示「当前可打的那一个难度」，所以难度编号看 text_guess（=本轮目标），
                # picked_index 只是可见列表里的第几个，不能当编号用。
                actual = parse_num(j.get("text_guess")) or this_num or \
                    (int(j.get("picked_index") or 0) or None)
                # 目标难度还没解锁时（大厅只画到 N8），实际打的就是最高的那一档，
                # 不能记成目标名，否则下一轮的后-A判定会以为已经推进了。
                # 2026-11-23 12:33 修：英雄模式第 1 行 = N11，比较可见行数要加 mode_base，
                # 否则 N11 会被误判成「目标未解锁」而改记成 N1（导致低难度规则错、跳过秘境）
                _mb = int(j.get("mode_base") or 0)
                rows_n = int(j.get("rows") or 0) + _mb
                if rows_n and actual and actual > rows_n:
                    print(f"   ℹ 大厅只解锁到 N{rows_n}（可见 {rows_n} 个按钮），"
                          f"目标 N{actual} 还没开 → 本轮按 N{rows_n} 算", flush=True)
                    actual = rows_n
                if j:
                    print(f"   🏷 大厅记录：可见 {j.get('rows')} 个难度按钮，点了第 "
                          f"{j.get('picked_index')} 个 → 记为 {j.get('text_guess')}"
                          f"  （兜底={bool(j.get('fallback'))} 高亮行={j.get('hl_index')}"
                          f" 识别数字={j.get('read_digit')})", flush=True)
                    sim = j.get("btn_sim_prev")
                    if sim is not None:
                        if sim > 0.93:
                            print(f"   ⚠ 难度按钮与上一轮（{j.get('btn_prev_tag')}）"
                                  f"相似度 {sim:.3f} → 看起来还是同一个难度，本轮可能没推进",
                                  flush=True)
                        else:
                            print(f"   ✅ 难度按钮与上一轮（{j.get('btn_prev_tag')}）"
                                  f"相似度 {sim:.3f} → 与上一轮不同，难度确实推进了", flush=True)
                if this_num and actual and actual != this_num:
                    print(f"   ⚠ 实际点第 {actual} 行 ≠ 目标第 {this_num} 行 → 按实际行号判定",
                          flush=True)
                if actual:
                    cur_num = actual
                    pause_s = int(dcfg.get("pause_after_slots", 60))
                    if low_rule and actual <= low_max:
                        print(f"   ℹ 本轮难度 N{actual} → 低难度：跳过秘境、"
                              f"存档后等 {pause_s}s 直接退出", flush=True)
                    else:
                        print(f"   ℹ 本轮难度 N{actual} → 正常打秘境", flush=True)
                    rec["实际难度"] = f"N{actual}"
                    r_skip = (low_rule and actual <= low_max
                              and bool(dcfg.get("skip_secret_on_low", True)))
                    r_quit = (low_rule and actual <= low_max
                              and bool(dcfg.get("quit_direct_on_low", True)))
                    r_pause = pause_s if (low_rule and actual <= low_max) else 0
                    r_wait = (pause_before_secret
                              if (secret_on and not r_skip) else 0)
                else:
                    print("   ℹ 本轮难度未识别（没读到大厅记录）→ 按原流程打秘境", flush=True)
            # 低难度：存档挑战打完等一会儿，等游戏把结算/奖励走完再退
            if k == "slots" and r_pause:
                print(f"   ⏳ 存档挑战已完成，按低难度规则等 {r_pause}s …", flush=True)
                if wait_quiet(r_pause, "等待退出期间"):
                    return login_abort("等待退出期间")
            # 高难度：存档挑战打完 → 等 pause_before_secret 秒再进秘境（用户 2026-11-17 16:13：30s）
            if k == "slots" and r_wait:
                print(f"   ⏳ 存档挑战已完成，等 {r_wait}s 再开始秘境挑战 …", flush=True)
                if wait_quiet(r_wait, "等待秘境挑战期间"):
                    return login_abort("等待秘境挑战期间")
        rec["总耗时"] = f"{time.time() - t0:.0f}s"
        rec["中断"] = "是" if aborted else "否"

        # ---------- 僵尸会话：等完还没释放 → 只能重启平台，直接停手 ----------
        if BUSY_HIT["v"]:
            all_rec.append(rec)
            print("", flush=True)
            print("=" * 62, flush=True)
            print("⛔ 平台一直是「您正在游戏中，不能创建游戏房间」→ 游戏会话卡在平台进程里", flush=True)
            _bw = root.get("busy_wait_minutes", 6)
            _ba = root.get("busy_attempts", 4)
            print(f"   已经按「搜图→建房」整段试过 {_ba} 次（间隔 {_bw} 分钟）并重启过平台，"
                  f"仍未释放。", flush=True)
            print("   请手动：① 关掉 KK 官方对战平台  ② 重新打开并登录  ③ 点「开始运行」", flush=True)
            print("   （本任务到此为止，不会再点任何东西）", flush=True)
            print("=" * 62, flush=True)
            return RC_BUSY

        # ---------- 登录窗口：停手，什么都不做（优先于重开）----------
        if LOGIN["ts"]:
            return login_abort(f"第 {i} 轮结束处")

        # ---------- 可恢复中断：单局超时 / 风控 / 游戏提前结束 → 杀 War3 → 重开 → 重跑同一轮 ----------
        # 风控策略见用户 19:06；「游戏已经结束 + 返回平台」见 2026-11-16 23:03。
        # 单局超时（用户 2026-11-17 15:20）：不再重启平台，改走同一条「杀 War3 → 重开游戏」的路。
        if ROUND_HIT["v"] or RISK_HIT["v"] or BACK_HIT["v"]:
            if ROUND_HIT["v"]:
                why = f"单局超 {ROUND['limit']} 分钟没打完"
                print(f"\n⏱ 第 {i} 轮已经打了 {time.time() - t0:.0f}s"
                      f"（上限 {ROUND['limit']} 分钟）还没打完一局 → 判定卡死", flush=True)
            elif RISK_HIT["v"]:
                why = "风控"
            else:
                why = "游戏提前结束(返回平台)"
            if risk_retry_left <= 0:
                # 2026-11-23 01:04 用户要求：重试 5 次仍失败 → 跳过这一局，继续下一局（不停止）
                rec["中断"] = f"是({why}，重试 {risk_retry} 次仍失败→跳过)"
                all_rec.append(rec)
                print(f"   ⏭ {why}：重试 {risk_retry} 次仍失败 → 跳过这一局，继续下一局", flush=True)
                continue
            risk_retry_left -= 1
            retry_streak += 1
            rec["中断"] = f"是({why}→重开)"
            all_rec.append(rec)
            print("   🔧 按设定结束 War3 进程 …（平台不动）", flush=True)
            cleared = kill_war3_and_wait()
            # 用户 01:21：从第 1 次重开起就固定等 20s，不再分段
            wait_s = int(risk_wait)
            print(f"   {'✅ War3 进程已清' if cleared else '⚠ War3 进程还有残留，仍继续尝试重开'}"
                  f"，等 {wait_s}s 后重开游戏（连续第 {retry_streak} 次重开）", flush=True)
            for _s in range(wait_s):
                time.sleep(1)
            RISK_HIT["v"] = False
            BACK_HIT["v"] = False
            ROUND_HIT["v"] = False
            i -= 1                     # 这一轮没打完 → 不推进，重跑同一个难度
            tail = ("不限次数，继续" if risk_unlimited
                    else f"剩余自动重开次数 {risk_retry_left}")
            print(f"   🔄 重新开始游戏，重跑{rnd(i + 1)}（{tail}）", flush=True)
            continue

        all_rec.append(rec)
        if rec.get("A 进场") == "成功":
            retry_streak = 0              # 这轮真进场了 → 连续重开计数归零
        if not aborted:
            close_popups("🪟 退出后随机弹窗检查")
        # 2026-11-26 23:50 用户定：**局间 ESC ×2 删除**。
        # 原因：正常退出游戏后平台返回的是房间窗（1224x904），房间窗上的 ESC = 退出房间
        # （第 1 次弹「是否确认退出房间？」，第 2 次确认），实测 8 秒后房间窗就没了 →
        # 下一轮判「没有房间」→ 搜地图建房 → 房间⇄详情页死循环。
        # 现在只保留 5 秒缓冲（等平台从游戏退回房间、画面刷新完），不再按 ESC。
        if i < rounds or forever:
            print("   🔙 本局结束：等 5s 让平台退回房间（不再按 ESC，避免把房间退掉）", flush=True)
            for _s in range(5):
                time.sleep(1)
                if LOGIN["ts"]:
                    return login_abort("局间等待时")
        # 局间冷却：连续跑多局、间隔又完全一致，最容易触发风控 → 留一段静默
        if i < rounds and cooldown > 0:
            print(f"   ⏸ 局间冷却 {cooldown}s（降低连续自动的识别风险）…", flush=True)
            for sec in range(cooldown):
                if sec and sec % 30 == 0:
                    print(f"      …还等 {cooldown - sec}s", flush=True)
                time.sleep(1)

    print(f"\n{'=' * 62}\n汇总\n{'-' * 62}")
    for i, rec in enumerate(all_rec, 1):
        rn = rec.get("轮次", i)
        tag = f"第 {rn} 轮" + (f"（第 {i} 次尝试）" if rn != i else "")
        print(f"{tag}：" + " | ".join(f"{k}={v}" for k, v in rec.items() if k != "轮次"))
    ok_n = sum(1 for r in all_rec if r.get("中断") == "否")
    print(f"{'-' * 62}\n完成 {ok_n}/{len(all_rec)} 次尝试\n{'=' * 62}", flush=True)
    return 0 if ok_n == len(all_rec) else 1


if __name__ == "__main__":
    sys.exit(main())
