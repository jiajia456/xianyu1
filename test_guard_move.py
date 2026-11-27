# -*- coding: utf-8 -*-
"""自检：登录守卫迁移到游戏监控（2026-11-19 20:00 用户定）

断言：
  ① watch_early.py 里守卫已内置：起线程、命中退出 131、大睡拆小步
  ② full_loop.py 的 stage_clear：进 ④ 停主进程守卫、finally 恢复、rc==131 接住
  ③ login_watch_start 幂等：连调两次不产生双线程（旧的先停）
  ④ login_watch_stop 对未启动状态安全
"""
import os
import sys
import io
import time
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

ok = True


def check(name, cond, extra=""):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + extra) if extra else ""))
    ok = ok and bool(cond)


print("[1] watch_early.py 守卫内置")
src = io.open("watch_early.py", encoding="utf-8").read()
check("import login_guard（带 try 兜底）",
      "import login_guard as lg" in src and "_LG_OK" in src)
check("守卫线程已启动（3.0s + ④ 游戏监控 标签）",
      "lg.watch_loop" in src and '"④ 游戏监控"' in src)
check("命中退出码 131（与 full_loop/fishing 同约定）", "return 131" in src)
check("主循环开头就查命中标志",
      src.find('if login_hit["ts"]:') < src.find("res = cycle(w, inp, args)"))
check("大睡拆 0.5s 小步（命中最多 0.5s 就退）",
      "_step = min(0.5, _left)" in src)
check("守卫注释记录了迁移决定", "2026-11-19 20:00 用户定" in src)

print("\n[2] full_loop.py stage_clear 接管/恢复")
src = io.open("full_loop.py", encoding="utf-8").read()
check("④ 进场先停主进程守卫",
      "login_watch_stop()" in src.split("def stage_clear")[1].split("def stage_slots")[0])
check("finally 恢复（成败都恢复，无守卫空窗）",
      "finally:\n        login_watch_start()" in
      src.split("def stage_clear")[1].split("def stage_slots")[0])
check("rc==131 接住并设 LOGIN[ts]",
      "if rc == RC_LOGIN:" in src.split("def stage_clear")[1].split("def stage_slots")[0]
      and 'rec["C 提前挑战"] = "中止(检测到登录窗口)"' in src)
_lws = src.split("def login_watch_start")[1].split("def login_watch_stop")[0]
check("login_watch_start 防重（开头先停旧的）", "login_watch_stop()" in _lws)

print("\n[3] 运行时行为：start 幂等 / stop 安全")
import full_loop as fl  # noqa: E402

fl.LOGIN["guard"] = None          # 未启动状态
try:
    fl.login_watch_stop()
    check("stop 对未启动状态安全（不抛异常）", True)
except Exception as e:
    check("stop 对未启动状态安全（不抛异常）", False, repr(e))

fl.login_watch_start()            # 起第一个
g1 = fl.LOGIN["guard"]
time.sleep(0.2)
fl.login_watch_start()            # 起第二个 → 旧的应被 set（防双线程）
g2 = fl.LOGIN["guard"]
check("第二次 start 换了新 Event", g2 is not g1, "g1=%s g2=%s" % (type(g1).__name__, type(g2).__name__))
check("旧线程的停止标志已被 set（不会双跑）", g1.is_set())
check("新线程活着（未 set）", not g2.is_set())
time.sleep(0.2)
fl.login_watch_stop()
check("stop 后标志 set", g2.is_set())

print("\n结果：" + ("全部通过" if ok else "❌ 有用例失败"))
sys.exit(0 if ok else 1)
