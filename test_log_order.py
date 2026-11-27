"""日志区「倒序显示」自检（离线，不需要开游戏）

用户 2026-11-17 20:05 要求：整个日志区「信息最新排第一」——
也就是说 write() 每写一次，那一块要出现在最上面，越新的越靠上。

覆盖 4 件事：
  ① 连续单行写入 → 最新一行在最上面
  ② 多行消息作为一块插入 → 块内顺序不变（不然提示会倒着读）
  ③ 超过行数上限 → 裁掉最旧的（在末尾），最新的必须还在
  ④ 用户停在顶部时自动跟随（返回 True）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 无窗口也能跑

from PyQt5.QtWidgets import QApplication, QPlainTextEdit   # noqa: E402

import app_gui   # noqa: E402


APP = None          # 必须留全局引用，否则 QApplication 被回收会把控件一起删掉


def new_edit():
    global APP
    if QApplication.instance() is None:
        APP = QApplication(sys.argv[:1])
    e = QPlainTextEdit()
    e.setReadOnly(True)
    e.setMaximumBlockCount(0)        # 和界面一致：交给 insert_block_at_top 自己裁
    return e


def lines_of(e):
    return e.toPlainText().splitlines()


def case(title, ok, detail=""):
    print(f"{'✅' if ok else '❌'} {title}" + (f"  {detail}" if detail else ""))
    return ok


def main():
    all_ok = True

    # ① 连续单行 → 最新的在最上面
    e = new_edit()
    for i in (1, 2, 3):
        app_gui.insert_block_at_top(e, f"第{i}行")
    got = lines_of(e)
    all_ok &= case("连续单行：最新在最上面", got == ["第3行", "第2行", "第1行"], str(got))

    # ② 多行块：块内顺序不变，新的块在上面
    e = new_edit()
    app_gui.insert_block_at_top(e, "A\nB\nC")
    app_gui.insert_block_at_top(e, "D")
    got = lines_of(e)
    all_ok &= case("多行块：块内顺序不变、新块在上", got == ["D", "A", "B", "C"], str(got))

    # ③ 超限裁剪：保留最新，丢掉最旧
    e = new_edit()
    for i in range(1, 11):                     # 写 10 行
        app_gui.insert_block_at_top(e, f"L{i}", max_lines=5)
    got = lines_of(e)
    want = ["L10", "L9", "L8", "L7", "L6"]     # 最新 5 行，倒序
    all_ok &= case("超过上限只留最新的 5 行", got == want, str(got))
    all_ok &= case("被裁掉的是最旧的 L1~L5", not any(x in got for x in ("L1", "L2", "L3", "L4", "L5")))

    # ④ 自动跟随
    e = new_edit()
    for i in range(1, 60):
        app_gui.insert_block_at_top(e, f"行{i}")
    bar = e.verticalScrollBar()
    all_ok &= case("自动跟随：滚动条停在顶部", bar.value() == bar.minimum(),
                   f"value={bar.value()} min={bar.minimum()}")

    # ⑤ 空行也能正常插（界面里有用 write("") 做分隔）
    e = new_edit()
    app_gui.insert_block_at_top(e, "上")
    app_gui.insert_block_at_top(e, "")
    app_gui.insert_block_at_top(e, "下")
    got = lines_of(e)
    all_ok &= case("空行分隔不报错", got[:3] == ["下", "", "上"], str(got))

    print()
    print("全部通过 ✅" if all_ok else "有失败项 ❌")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
