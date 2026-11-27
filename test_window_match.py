# -*- coding: utf-8 -*-
"""找窗口的判定自检（离线，不碰游戏）

背景（2026-11-17 19:11 用户现场）：另一台电脑上游戏已经开起来了，主程序却一直打
「等窗口出现」，等了 246 秒还没绑定。原因是三件事叠加：
  1. 旧代码要求「exe 名命中 **且** 标题命中 Warcraft」——中文标题/带版本号的 exe 直接出局
  2. 最小化到屏幕外的窗口（GetWindowRect ≈ -32000）被整个丢弃
  3. 等待循环把真实异常吞了，只打一行没信息量的「等窗口出现」

本测试用假窗口列表跑 GameWindow._find，覆盖这几种情况。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import game_auto as ga

FAIL = []


def check(name, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {name}{('  ' + extra) if extra else ''}")
    if not cond:
        FAIL.append(name)


def win(hwnd, exe, title, w=1690, h=921, off=False, iconic=False):
    return {"hwnd": hwnd, "title": title, "exe": exe, "w": w, "h": h,
            "pid": 0, "offscreen": off, "iconic": iconic}


CFG = dict(exe=["War3.exe", "Warcraft III.exe", "y3.exe"], title_re=["Warcraft"])


def find(wins, **over):
    """用给定窗口列表跑一次 _find；返回 (hwnd 或 None, 异常文本)"""
    ga.enum_windows_full = lambda: wins
    cfg = dict(CFG)
    cfg.update(over)
    win_kw = dict(title_re=cfg["title_re"], exe=cfg["exe"],
                  min_size=cfg.get("min_size", 400), pick="largest")
    try:
        w = ga.GameWindow.__new__(ga.GameWindow)
        w.title_res = [__import__("re").compile(t, __import__("re").I)
                       for t in (win_kw["title_re"] or [])]
        w.exe_names = [e.lower() for e in (win_kw["exe"] or [])]
        w.min_size = win_kw["min_size"]
        w.pick = win_kw["pick"]
        w.pid = 0
        return w._find(), ""
    except Exception as e:
        return None, str(e)


# 1. 正常：exe + 英文标题都对
h, err = find([win(101, "War3.exe", "Warcraft III")])
check("① exe 与标题都对 → 绑定", h == 101, err[:80])

# 2. 旧逻辑的死穴：exe 对、标题是中文（不含 Warcraft）
h, err = find([win(102, "War3.exe", "魔兽争霸III：冰封王座")])
check("② 标题是中文、exe 对 → 绑定（旧逻辑会失败）", h == 102, err[:80])

# 3. exe 名字带版本号、标题英文
h, err = find([win(103, "war3 1.27.exe", "Warcraft III Frozen Throne")])
check("③ exe 名带版本号、标题对 → 绑定（旧逻辑会失败）", h == 103, err[:80])

# 4. 最小化到屏幕外：仍要能绑定（并尝试还原）
h, err = find([win(104, "War3.exe", "Warcraft III", off=True, iconic=True)])
check("④ 窗口最小化到屏幕外 → 仍能绑定", h == 104, err[:80])

# 5. 正常窗口与最小化窗口同时存在 → 优先正常窗口
h, err = find([win(105, "War3.exe", "Warcraft III", off=True, iconic=True),
               win(106, "War3.exe", "Warcraft III", w=1200, h=700)])
check("⑤ 有正常窗口时优先正常窗口", h == 106, err[:80])

# 6. 尺寸太小 → 报错里点明「尺寸被过滤」
h, err = find([win(107, "War3.exe", "Warcraft III", w=300, h=200)])
check("⑥ 窗口太小 → 报错说清是尺寸问题", h is None and "尺寸太小" in err, err[:70])

# 7. 名字完全不匹配 → 报错带窗口清单（用户能直接把这一行发回来）
h, err = find([win(108, "FrozenThrone.exe", "魔兽争霸"),
               win(109, "chrome.exe", "新标签页", w=1200, h=800)])
check("⑦ 名字标题都不对 → 报错附可见窗口清单",
      h is None and "FrozenThrone.exe" in err, err[:90])

# 8. 只有无关窗口时，清单里能看到最像游戏的（面积大的排前面）
h, err = find([win(110, "notepad.exe", "无标题", w=400, h=300),
               win(111, "game.exe", "某游戏", w=1900, h=1000)])
check("⑧ 清单按面积降序，游戏排第一", h is None and err.index("game.exe") < err.index("notepad.exe"),
      err[:90])

print()
if FAIL:
    print("失败项：", FAIL)
    raise SystemExit(1)
print("窗口匹配自检全部通过")
