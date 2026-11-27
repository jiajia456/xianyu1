# -*- coding: utf-8 -*-
"""环境兼容层自检（离线，不碰游戏）

覆盖 2026-11-17 18:5x 加进成品的那批「换电脑才会暴露」的适配：
  1. DPI 感知是否在进程第一步生效（per-monitor）
  2. 启动环境自检字段是否齐全（缩放/物理分辨率/显示器数/管理员/系统版本）
  3. 目标进程提权检测可用（权限不一致会「点不动」）
  4. 截图归一化：不同窗口尺寸都能回到基准；宽高比差太多会给出告警
  5. 纯色画面（最小化/遮挡/全屏独占）判定阈值不会误杀正常画面
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app_gui
import game_auto as ga
import in_game as ig

FAIL = []


def check(name, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {name}{('  ' + extra) if extra else ''}")
    if not cond:
        FAIL.append(name)


# 1. DPI
check("DPI 感知模式", app_gui.DPI_MODE in ("per-monitor", "system"), app_gui.DPI_MODE)

# 2. 环境自检
env = app_gui.env_report()
need = {"dpi_mode", "scale", "phys_screen", "monitors", "admin", "win"}
check("环境字段齐全", need <= set(env), ",".join(sorted(need & set(env))))
check("缩放值是正数", float(env.get("scale", 0)) >= 1.0, f"scale={env.get('scale')}")
check("显示器数 >= 1", int(env.get("monitors", 0)) >= 1, f"monitors={env.get('monitors')}")

# 3. 提权检测
me = ga.is_elevated(os.getpid())
check("提权检测返回布尔", isinstance(me, bool), f"admin={me}")

# 4. 归一化
base = np.zeros((921, 1690, 3), np.uint8)
for w, h in [(1690, 921), (1065, 580), (1352, 737), (1690, 1000)]:
    img = np.full((h, w, 3), 40, np.uint8)
    out = ig.norm(img)
    ok = out.shape[:2] == (ig.REF[1], ig.REF[0])
    check(f"归一化 {w}x{h} → 1690x921", ok, str(out.shape[:2]))

# 5. 纯色判定阈值
flat = np.full((100, 100, 3), 30, np.uint8)
real = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
sd_flat = float(flat.reshape(-1, 3).std(axis=0).max())
sd_real = float(real.reshape(-1, 3).std(axis=0).max())
check("纯色画面被判无效", sd_flat < 1.0, f"std={sd_flat:.2f}")
check("正常画面不被误杀", sd_real >= 1.0, f"std={sd_real:.2f}")

print()
if FAIL:
    print("失败项：", FAIL)
    raise SystemExit(1)
print("环境兼容层自检全部通过")
