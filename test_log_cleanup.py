# -*- coding: utf-8 -*-
"""自检：启动时清理 3 天前的一次性日志（2026-11-19 21:05 用户定）

用临时目录做 root，验证：
  ① 5 天前的 .log / .png 被删
  ② 2 天前的 .log / .png 保留
  ③ secret_frame_*.png（秘境滑窗自管）保留
  ④ gui_console.log 保留
  ⑤ *.json / *.jsonl 保留
  ⑥ 子目录不被动（diff_rows 原样）
  ⑦ 返回值 (个数, 字节) 正确
"""
import os
import sys
import time
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import app_gui  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + extra) if extra else ""))
    ok = ok and bool(cond)


tmp = tempfile.mkdtemp(prefix="kk_logclean_")
old = time.time() - 5 * 86400      # 5 天前
new = time.time() - 2 * 86400      # 2 天前


def touch(name, mtime, size=1024):
    p = os.path.join(tmp, name)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    os.utime(p, (mtime, mtime))
    return p


# 旧文件（该删）
touch("bot_20261110_120000.log", old)
touch("early_watch_20261110_1200.log", old)
touch("sign_panel_111000.png", old)
touch("claim_y544_page.png", old)
touch("fishing_enter_111000.png", old)
# 新文件（该留）
touch("bot_20261119_120000.log", new)
touch("task_before.png", new)
# 例外保留
touch("gui_console.log", old)               # 正在写，例外
touch("secret_frame_111000.png", old)       # 秘境滑窗自管，例外
touch("login_events.jsonl", old)            # 事件流水，例外
touch("lobby_difficulty.json", old)         # json 不清
# 子目录（不该被动）
os.mkdir(os.path.join(tmp, "diff_rows"))
with open(os.path.join(tmp, "diff_rows", "lobby_full.png"), "wb") as f:
    f.write(b"x" * 512)
os.utime(os.path.join(tmp, "diff_rows", "lobby_full.png"), (old, old))

n, freed = app_gui.cleanup_old_logs(days=3, root=tmp)

left = set(os.listdir(tmp))
check("5 天前的任务日志被删", "bot_20261110_120000.log" not in left)
check("5 天前的监控日志被删", "early_watch_20261110_1200.log" not in left)
check("5 天前的签到截图被删", "sign_panel_111000.png" not in left)
check("5 天前的领券截图被删", "claim_y544_page.png" not in left)
check("5 天前的钓鱼截图被删", "fishing_enter_111000.png" not in left)
check("2 天前的任务日志保留", "bot_20261119_120000.log" in left)
check("2 天前的截图保留", "task_before.png" in left)
check("gui_console.log 例外保留", "gui_console.log" in left)
check("secret_frame_*.png 例外保留", "secret_frame_111000.png" in left)
check("*.jsonl 事件流水保留", "login_events.jsonl" in left)
check("*.json 不清", "lobby_difficulty.json" in left)
check("子目录 diff_rows 没被动",
      os.path.exists(os.path.join(tmp, "diff_rows", "lobby_full.png")))
check("返回删除个数 = 5", n == 5, "n=%d" % n)
check("返回释放字节 = 5×1024", freed == 5 * 1024, "freed=%d" % freed)

# 再跑一遍：没有可删的 → (0, 0)，不报错
n2, freed2 = app_gui.cleanup_old_logs(days=3, root=tmp)
check("第二次跑幂等（0 个可删）", n2 == 0 and freed2 == 0)

# 不存在的目录 → 安全返回
n3, freed3 = app_gui.cleanup_old_logs(days=3, root=os.path.join(tmp, "no_such"))
check("不存在的目录安全返回 (0,0)", (n3, freed3) == (0, 0))

# 清理临时目录
import shutil
shutil.rmtree(tmp, ignore_errors=True)

print("\n结果：" + ("全部通过" if ok else "❌ 有用例失败"))
sys.exit(0 if ok else 1)
