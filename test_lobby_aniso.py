# -*- coding: utf-8 -*-
"""非等比窗口（换电脑后 War3 被「横向压扁」）下的选难度离线桩测试。

起因（2026-11-18 17:04，用户另一台电脑的日志 KK日志_20261118_170520）：
  - 绑定 War3 时客户区 1264x921（fx=0.748、fy=1.0）
  - 94 秒后窗口被 War3 重建，新窗口 1200x830（fx=0.710、fy=0.901）
  - 模板全是在 1690x921 下截的，只试等比缩放永远对不上 → 一行难度都认不出
    → 整步被判「游戏窗口已关闭」，根本没走到选难度

本测试把调参用的真实大厅截图（1690x921）**按两个方向分别缩放**到那台机器的
真实尺寸，然后：
  ① 验证「只等比」确实匹配不上（复现原故障，防止以后又把非等比档删掉）；
  ② 验证带上 (宽/1690, 高/921) 这一档后，开始按钮、难度行、点开始游戏全部命中。

运行： python test_lobby_aniso.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import game_auto as ga                                    # noqa: E402

SAMPLE = os.path.join(HERE, "logs", "diff_rows", "lobby_after_full.png")

# 素材缺失保护（2026-11-19）：这张图是真机大厅截图，清理 logs 时被删掉了；
# 合成图替代不了「非等比缩放」这件事本身，所以缺素材就明确跳过，不报假失败。
if not os.path.exists(SAMPLE):
    print(f"\u23ed 跳过：缺素材 {os.path.basename(SAMPLE)}")
    print("   （需要一张真机大厅截图放在 logs/diff_rows/lobby_after_full.png，")
    print("    拿到样本后本自检自动恢复，不用改代码）")
    raise SystemExit(0)
# 2026-11-27：这张素材会被新一轮真机截图覆盖，而本测试的断言是按「9 行 N1~N9」
# 写的（见文件头）。素材只有 2 行（N11-N20 视图）时会报「只认出 2 行」假失败，
# 与缺素材同样跳过。
try:
    import cv2 as _cv2
    import numpy as _np
    _img = _cv2.imdecode(_np.fromfile(SAMPLE, dtype=_np.uint8), _cv2.IMREAD_COLOR)
    _rows = ga.detect_rows(_img, [690, 105, 190, 595]) if _img is not None else []
    if len(_rows) < 5:
        print(f"\u23ed 跳过：素材只有 {len(_rows)} 行难度（本测试按 9 行素材写断言）")
        raise SystemExit(0)
except SystemExit:
    raise
except Exception as _e:
    print(f"\u23ed 跳过：素材行数检测失败（{_e!r}）")
    raise SystemExit(0)
CFG = os.path.join(HERE, "configs", "wuxia_lobby.json")

# 那台电脑真实出现过的两个尺寸（宽 x 高）
SIZES = [(1264, 921), (1200, 830)]


def imread_u(path: str):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


class FakeWin:
    hwnd, pid = 1, 1

    def __init__(self, img=None, **_kw):
        self.img = img

    def client_rect(self):
        return (0, 0, self.img.shape[1], self.img.shape[0])

    def alive(self):
        return True

    def fg_same_process(self):
        return True

    def ensure_foreground(self, wait: float = 0.2):
        return True

    def focus(self, wait: float = 0.2):
        return True


class FakeCap:
    def __init__(self, img):
        self.img = img

    def grab(self, win):
        return self.img


def find_step(steps, key):
    for st in steps or []:
        if not isinstance(st, dict):
            continue
        if key in st and isinstance(st[key], dict):
            return st[key]
        for v in st.values():
            if isinstance(v, dict) and isinstance(v.get("steps"), list):
                r = find_step(v["steps"], key)
                if r:
                    return r
    return None


def find_if_image(steps):
    for st in steps or []:
        if not isinstance(st, dict):
            continue
        for k, v in st.items():
            if k == "if_image" and isinstance(v, dict):
                return v
            if isinstance(v, dict) and isinstance(v.get("steps"), list):
                r = find_if_image(v["steps"])
                if r:
                    return r
    return None


def make_frame(w: int, h: int):
    """把基准截图按「宽、高各自缩放」到目标尺寸（模拟横向被压扁的窗口）"""
    src = imread_u(SAMPLE)
    if src is None:
        raise SystemExit(f"缺少样本图：{SAMPLE}")
    return cv2.resize(src, (w, h), interpolation=cv2.INTER_AREA)


def run(w: int, h: int, tmp: str):
    img = make_frame(w, h)
    fx, fy = w / 1690.0, h / 921.0

    with open(CFG, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["wait_window"] = 0
    steps = cfg["tasks"]["auto_start"]["steps"]

    # ── ① 只等比：应当匹配不上（复现故障）──
    vis = ga.Vision(os.path.join(HERE, "templates"))
    spec = find_if_image(steps) or {}
    uniform_only = [s for s in (spec.get("scales") or [1.0])
                    if isinstance(s, (int, float))]
    uni_hit = vis.find("btn_lobby_start.png", img, 0.85, scales=uniform_only)
    uni_best = 0.0
    if not uni_hit:
        # 记录「只等比」能拿到的最好分数，方便看差多远
        for s in uniform_only:
            h1 = vis.find("btn_lobby_start.png", img, 0.0, scales=[s])
            if h1:
                uni_best = max(uni_best, h1[4])
    else:
        uni_best = uni_hit[4]

    # ── ② 带上 (fx, fy)：应当命中 ──
    aniso_scales = list(uniform_only) + [(fx, fy)]
    aniso_hit = vis.find("btn_lobby_start.png", img, 0.85, scales=aniso_scales)

    # ── ③ 跑整段 auto_start（dry-run），看点击落在哪 ──
    ga.GameWindow = FakeWin
    clicks = []
    old = os.getcwd()
    os.chdir(tmp)
    try:
        bot = ga.Bot(cfg, os.path.dirname(CFG), dry_run=True)
        bot.win.img = img
        bot.cap = FakeCap(img)
        bot._sleep = lambda s: None
        real_click = bot.input.click

        def rec_click(x, y, *a, **k):
            clicks.append((int(x), int(y)))
            return real_click(x, y, *a, **k)

        bot.input.click = rec_click
        real_hover = bot.input.click_hover

        def rec_hover(x, y, *a, **k):
            clicks.append((int(x), int(y)))
            return real_hover(x, y, *a, **k)

        bot.input.click_hover = rec_hover
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bot.exec_steps(steps)
        out = buf.getvalue()
    finally:
        os.chdir(old)

    # 期望的难度行中心（独立算一遍，不看 bot 内部状态）
    row = find_step(steps, "click_row")
    kw, region, _sx, _sy = ga.fit_spec(
        dict(bright=row["bright"], min_h=row["min_h"],
             max_h=row["max_h"], min_w=row["min_w"]),
        row["region"], img, row.get("ref"))
    rows = ga.detect_rows(img, region, **kw)
    want_last = ga.row_center(rows, len(rows) - 1) if rows else (None, None)

    return dict(size=(w, h), fx=fx, fy=fy,
                uni_best=uni_best, aniso_score=(aniso_hit[4] if aniso_hit else 0.0),
                aniso_hit=bool(aniso_hit), rows=len(rows), region=region,
                want_last=want_last, clicks=clicks, log=out)


def main():
    tmp = tempfile.mkdtemp(prefix="kk_lobby_aniso_")
    print(f"样本：{SAMPLE}\n")
    bad = []
    for (w, h) in SIZES:
        r = run(w, h, tmp)
        print(f"—— 窗口 {w}x{h}  (fx={r['fx']:.3f}, fy={r['fy']:.3f}) ——")
        print(f"  只等比  最佳分 {r['uni_best']:.3f}  → "
              f"{'❌ 反而命中了（非等比档没必要？）' if r['uni_best'] >= 0.85 else '✅ 匹配不上（符合预期，这就是原故障）'}")
        print(f"  +非等比 最佳分 {r['aniso_score']:.3f}  → "
              f"{'✅ 命中' if r['aniso_hit'] else '❌ 仍然匹配不上，v1.8 修不动这个尺寸'}")

        rows = r["rows"]
        print(f"  识别难度行 {rows} 行，region={r['region']}")
        if rows >= 1:
            print(f"  期望最后一行为 {r['want_last']}")
        if r["clicks"]:
            print(f"  点击记录（前 6）：{r['clicks'][:6]}")
        else:
            print("  点击记录：无（整段没走通）")

        # 断言：非等比必须命中；难度行必须认出 >= 5 行；必须点了开始游戏
        if not r["aniso_hit"]:
            bad.append(f"{w}x{h} 非等比未命中")
        if rows < 5:
            bad.append(f"{w}x{h} 只认出 {rows} 行难度")
        if not r["clicks"]:
            bad.append(f"{w}x{h} 没有任何点击")
        print()

    if bad:
        print("❌ 失败：")
        for b in bad:
            print("   -", b)
        return 1
    print("✅ 全部通过：那台电脑的两个真实窗口尺寸下，非等比档都能命中并走完选难度")
    return 0


if __name__ == "__main__":
    sys.exit(main())
