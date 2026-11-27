# -*- coding: utf-8 -*-
"""自检：①、② 要处理「您正在房间内…」，建房子弹窗仍不能被误点。

现场（2026-11-27 18:22 / 18:27，v2.20）：
  ② 点「创建房间」→ 建房子弹窗（「创建」+「取消」）刚出来
  → pipeline 每步收尾的 handle_popup 把这对按钮当成「平台提示 确定/取消」
  → 点「确定」把弹窗关掉 → ③ 填密码整步没跑 → 退房重建 → 死循环。

② 也要检测提示，但扫描时须跳过独立的建房子弹窗。
"""
from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FAILED = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (("  " + detail) if detail else ""))
    if not cond:
        FAILED.append(name)


def main():
    print("① pipeline.json：①、② 开 check_warn")
    with io.open(os.path.join(HERE, "configs", "pipeline.json"), encoding="utf-8") as f:
        pcfg = json.load(f)
    steps = pcfg["steps"]
    on = [s["name"] for s in steps if s.get("check_warn")]
    check("check_warn 开在 ①、②", on == ["① 搜地图并进详情页", "② 创建房间"], str(on))

    create = next(s for s in steps if s.get("task") == "create_room")
    check("② create_room 开 check_warn", create.get("check_warn"))
    fill = next(s for s in steps if s.get("task") == "fill_and_submit")
    check("③ fill_and_submit 没有 check_warn", not fill.get("check_warn"))

    print("② game_auto.py：收尾检测由 st['check_warn'] 控制")
    src = io.open(os.path.join(HERE, "game_auto.py"), encoding="utf-8").read()
    check("收尾检测前有 check_warn 判断", 'if st.get("check_warn"):' in src)
    # 检测块必须在 check_warn 里面（缩进对齐）
    i = src.find('if st.get("check_warn"):')
    blk = src[i:i + 900]
    check("handle_popup 在 check_warn 块内",
          "handle_popup(prefer_cancel=False" in blk)
    check("先处理提示再检查建房子弹窗",
          src.find('if st.get("check_warn"):') < src.find('if "②" in name and not create_dialog_hwnd():'))

    finder = io.open(os.path.join(HERE, "room_finder.py"), encoding="utf-8").read()
    check("扫描跳过建房子弹窗尺寸", "if 420 <= cw <= 800 and 350 <= ch <= 660:" in finder)

    print("③ ③ 的建房子弹窗仍是独立小窗（584x517，pick=smallest）")
    with io.open(os.path.join(HERE, "configs", "wuxia_dialog.json"), encoding="utf-8") as f:
        dcfg = json.load(f)
    check("pick=smallest", dcfg["window"]["pick"] == "smallest")
    check("fill_and_submit 存在", "fill_and_submit" in dcfg["tasks"])

    print()
    if FAILED:
        print("❌ 失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
