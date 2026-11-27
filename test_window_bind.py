# -*- coding: utf-8 -*-
"""窗口绑定自检：只有 War3 能被当成游戏窗口 + 纯色块不产生假命中"""
import os
import sys
import types

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import game_auto as ga

OK, BAD = 0, []


def ck(name, cond, extra=""):
    global OK
    if cond:
        OK += 1
        print(f"  ✅ {name} {extra}")
    else:
        BAD.append(name)
        print(f"  ❌ {name} {extra}")


def win(exe, title, w=1438, h=900, cls="Chrome_WidgetWin_1"):
    return {"hwnd": 1, "exe": exe, "title": title, "w": w, "h": h,
            "cls": cls, "pid": 1, "offscreen": False, "iconic": False}


print("\n[A] exe 白名单")
ck("War3.exe 通过", ga.is_game_candidate(win("War3.exe", "Warcraft III", 1690, 921, "Warcraft III")))
ck("war3.exe（小写）通过", ga.is_game_candidate(win("war3.exe", "Warcraft III")))
ck("Warcraft III.exe 通过", ga.is_game_candidate(win("Warcraft III.exe", "Warcraft III")))
ck("WorkBuddy.exe 被排除（本次事故窗口）", not ga.is_game_candidate(win("WorkBuddy.exe", "WorkBuddy")))
ck("chrome.exe 被排除", not ga.is_game_candidate(win("chrome.exe", "新标签页")))
ck("Code.exe 被排除", not ga.is_game_candidate(win("Code.exe", "main.py")))
ck("Platform.exe 被排除", not ga.is_game_candidate(win("Platform.exe", "KK官方对战平台", 1332, 945, "Qt5152QWindowIcon")))
ck("QQ.exe 被排除", not ga.is_game_candidate(win("QQ.exe", "QQ")))
ck("太小的窗口被排除", not ga.is_game_candidate(win("War3.exe", "Warcraft III", 200, 150)))

print("\n[B] exe 取不到时退回黑名单")
ck("标题含「对战平台」被排除", not ga.is_game_candidate(win("", "KK官方对战平台", 1332, 945, "")))
ck("标题含「三大流程循环」被排除", not ga.is_game_candidate(win("", "我在峡谷打僵尸 · 三大流程循环", 800, 600, "")))
ck("Qt 窗口被排除", not ga.is_game_candidate(win("", "某个窗口", 800, 600, "Qt5152QWindowIcon")))
ck("无 exe 的普通大窗口放行（保持原行为）", ga.is_game_candidate(win("", "Warcraft III", 800, 600, "Warcraft III")))

print("\n[C] 纯色块假阳性保护")
tpl = cv2.imdecode(np.fromfile(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "btn_lobby_start.png"),
    np.uint8), cv2.IMREAD_COLOR)
img_flat = np.full((300, 400, 3), 220, np.uint8)      # 纯色画面
hit = (50, 60, tpl.shape[1], tpl.shape[0], 1.0)
ck("_flat_patch 认得出纯色", ga.Vision._flat_patch(img_flat, hit, 0, 0) is True)

rng = np.random.default_rng(9)
img_tex = cv2.GaussianBlur(rng.integers(20, 240, (300, 400, 3), dtype=np.uint8), (0, 0), 2)
ck("_flat_patch 不误判有内容的画面", ga.Vision._flat_patch(img_tex, hit, 0, 0) is False)


class V(ga.Vision):
    def __init__(self, t):
        self.template_dir = ""
        self._cache = {}
        self._tpl = t

    def _load(self, name):
        return self._tpl


# 纯色画面 + 模板有内容 → 必须不命中（这正是 WorkBuddy 那次 score=1.000 的情形）
hit_flat = V(tpl).find("t.png", img_flat, 0.85, None, [1.0])
ck("纯色画面上不返回命中", hit_flat is None,
   f"score={hit_flat[4]:.3f}" if hit_flat else "None")

# 真的贴上按钮 → 仍要命中
img_real = img_tex.copy()
img_real[100:100 + tpl.shape[0], 150:150 + tpl.shape[1]] = tpl
hit_real = V(tpl).find("t.png", img_real, 0.85, None, [1.0])
ck("正常画面仍能命中", hit_real is not None, f"score={hit_real[4]:.3f}" if hit_real else "None")

print("\n[D] 枚举当前真实窗口")
ws = ga.enum_windows_full()
print(f"  共 {len(ws)} 个可见窗口")
for w in ws:
    if w["w"] >= 400 and w["h"] >= 400:
        mark = "✅ 候选" if ga.is_game_candidate(w) else "❌ 排除"
        print(f"   {mark}  {w.get('exe')!r:22s} {w['w']}x{w['h']}  {w.get('title','')[:28]!r}")
buddy = [w for w in ws if (w.get("exe") or "").lower() == "workbuddy.exe"]
ck("当前 WorkBuddy 窗口全部被排除", all(not ga.is_game_candidate(w) for w in buddy),
   f"{len(buddy)} 个")

print(f"\n{'='*46}")
if BAD:
    print(f"❌ 失败 {len(BAD)} 项: {BAD}")
    sys.exit(1)
print(f"✅ 全部通过（{OK} 项）")
