# -*- coding: utf-8 -*-
"""不同窗口尺寸下的「选难度 + 点开始游戏」离线桩测试。

用户 2026-11-17 18:32 在另一台电脑实测：游戏窗口比调参那台小（约 0.63 倍），
脚本卡在难度选择界面 —— 一行难度都识别不到，「开始游戏」按钮也等不到。

本测试用调参时的真实大厅截图（1690x921）缩放到 0.55 / 0.63 / 0.7 / 0.8 / 1.0 倍，
跑完整的 auto_start 任务（dry-run，只记录点击坐标、不真点、不真等），断言：
  ① 难度行全部识别出来（当前素材 9 行 N1~N9）；
  ② 第一次点击落在**最后一行**中心（= 最高可见难度，2026-11-19 素材里有 N9）；
  ③ 「开始游戏」按钮能命中，点击落在按钮中心（期望值用 1.0 素材的按钮中心按比例缩放，
     不再对缩放后的小图重新匹配 —— 0.55 倍时模板细节糊掉，独立匹配会假失败）。

运行： python test_lobby_scale.py
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


class FakeWin:
    """假窗口：只提供 dry-run 需要的那几个方法"""
    hwnd = 1
    pid = 1

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
    """假截图：永远返回同一帧（就是缩放后的那张大厅图）"""
    def __init__(self, img):
        self.img = img
        self.n = 0

    def grab(self, win):
        self.n += 1
        return self.img


def imread_u(path: str):
    """中文路径安全读图（cv2.imread 在含中文的路径上会失败）"""
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def find_step(steps, key):
    """在任务步骤里递归找第一个指定 key 的配置（不按索引，步骤增删不会失效）"""
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
    """在任务步骤里递归找第一个 if_image 配置（按钮那步）"""
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


_BTN_REF = None


def _btn_ref_center():
    """1.0 素材上「开始游戏」按钮中心（模板基准就是 1690x921，只算一次）"""
    global _BTN_REF
    if _BTN_REF is None:
        src = imread_u(SAMPLE)
        vis = ga.Vision(os.path.join(HERE, "templates"))
        h = vis.find("btn_lobby_start.png", src, 0.86)
        _BTN_REF = (h[0] + h[2] // 2, h[1] + h[3] // 2) if h else (None, None)
        print(f"    （参考按钮中心 @1.0 = {_BTN_REF}）")
    return _BTN_REF


def run_once(scale: float, tmp: str, drop_card: bool = False):
    src = imread_u(SAMPLE)
    if src is None:
        raise SystemExit(f"缺少样本图：{SAMPLE}")
    img = src if abs(scale - 1.0) < 1e-6 else cv2.resize(
        src, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    if drop_card:
        # 模拟「地图卡片还没加载出来」：把卡片那块涂成背景色
        y0, y1 = int(97 * scale), int(223 * scale)
        x0, x1 = int(425 * scale), int(672 * scale)
        img = img.copy()
        img[y0:y1, x0:x1] = (40, 38, 42)

    ga.GameWindow = FakeWin
    with open(CFG, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["wait_window"] = 0
    steps = cfg["tasks"]["auto_start"]["steps"]
    if drop_card:
        # 卡片不在时不必真等 25 秒，缩短超时；其余行为保持原样
        for st in steps:
            v = st.get("wait_image")
            if isinstance(v, dict) and v.get("template") == "card_mode_normal.png":
                v["timeout"] = 1200
                v["interval"] = 200
                break

    old = os.getcwd()
    os.chdir(tmp)
    try:
        bot = ga.Bot(cfg, os.path.dirname(CFG), dry_run=True)
        bot.win.img = img
        bot.cap = FakeCap(img)
        bot._sleep = lambda s: None            # 跳过所有等待，测试秒级跑完

        clicks = []
        real_click = bot.input.click

        def rec_click(x, y, *a, **k):
            clicks.append((int(x), int(y)))
            return real_click(x, y, *a, **k)

        bot.input.click = rec_click

        # 2026-11-18：难度点击已改成 click_hover（先停 0.7s 让 War3 收到鼠标进入事件
        # 再按下，直接 move+click 会被吞）。dry_run 下 click_hover 直接 return、不走
        # input.click —— 不同时 patch 的话 clicks 里只剩「点开始游戏」，
        # 断言会拿开始按钮的坐标去比难度坐标（假失败）。
        real_hover = bot.input.click_hover

        def rec_hover(x, y, *a, **k):
            clicks.append((int(x), int(y)))
            return real_hover(x, y, *a, **k)

        bot.input.click_hover = rec_hover
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bot.exec_steps(cfg["tasks"]["auto_start"]["steps"])
        out = buf.getvalue()
    finally:
        os.chdir(old)

    # 独立算一遍「最后一行中心」与「开始游戏按钮中心」作为期望值
    row = find_step(cfg["tasks"]["auto_start"]["steps"], "click_row")
    kw, region, sx, sy = ga.fit_spec(
        dict(bright=row["bright"], min_h=row["min_h"],
             max_h=row["max_h"], min_w=row["min_w"]),
        row["region"], img, row.get("ref"))
    rows = ga.detect_rows(img, region, **kw)
    # 最后一行 = 最下面一行 = 最高可见难度（新增 N9 后仍是这条规则，以后 N10/N11 也自动适用）
    want = ga.row_center(rows, len(rows) - 1) if rows else (None, None)

    vis = ga.Vision(os.path.join(HERE, "templates"))
    _btn_spec = find_if_image(cfg["tasks"]["auto_start"]["steps"]) or {}
    # 期望值：先在 1.0 素材上匹配一次按钮中心，再按比例缩到当前尺寸。
    # 不能直接对缩放后的小图重新匹配：0.55 倍时按钮细节糊掉，匹配分可能掉到 0，
    # 那样测的就不再是「坐标缩放对不对」，而是「小图还认不认得出按钮」。
    rx, ry = _btn_ref_center()
    btn = (int(round(rx * scale)), int(round(ry * scale)))         if rx is not None else (None, None)
    hit = vis.find("btn_lobby_start.png", img, 0.86,
                   scales=_btn_spec.get("scales"))

    # 地图卡片「普通模式」是否按预期被识别
    card_spec = find_step(cfg["tasks"]["auto_start"]["steps"], "wait_image")
    # 同样要带上 (scale, scale) 这一档：0.5x 粗筛只探「1.0 + 第一个二元档」，
    # 裸 scales 列表会让它在非 1.0 缩放下提前判「没有」（生产路径靠 cap_ref 自动补这一档）
    _cs = list((card_spec or {}).get("scales") or [1.0])
    if abs(scale - 1.0) > 0.02:
        _cs.insert(1, (scale, scale))
    card_hit = vis.find("card_mode_normal.png", img, 0.82, scales=_cs)

    return dict(scale=scale, size=(img.shape[1], img.shape[0]), rows=len(rows),
                want=want, btn=btn, btn_score=(hit[4] if hit else 0.0),
                card_score=(card_hit[4] if card_hit else 0.0),
                clicks=clicks, log=out)


def main():
    tmp = tempfile.mkdtemp(prefix="kk_lobby_scale_")
    ok_all = True
    print(f"样本：{SAMPLE}\n临时目录：{tmp}\n")
    print("—— 场景 A：地图卡片「普通模式」已出现（正常情况）——")
    for scale in (1.0, 0.8, 0.7, 0.63, 0.55):
        r = run_once(scale, tmp)
        w_pick = r["clicks"][0] if r["clicks"] else (None, None)
        w_btn = r["clicks"][1] if len(r["clicks"]) > 1 else (None, None)
        d_row = (abs(w_pick[0] - r["want"][0]) <= 3 and abs(w_pick[1] - r["want"][1]) <= 3) \
            if None not in r["want"] and None not in w_pick else False
        d_btn = (abs(w_btn[0] - r["btn"][0]) <= 3 and abs(w_btn[1] - r["btn"][1]) <= 3) \
            if None not in r["btn"] and None not in w_btn else False
        d_card = r["card_score"] >= 0.82
        ok = r["rows"] >= 8 and d_row and d_btn and d_card
        ok_all = ok_all and ok
        print(f"{'✅' if ok else '❌'} 缩放 {scale:<5} 窗口 {r['size'][0]}x{r['size'][1]}"
              f" | 卡片 score={r['card_score']:.3f} {'命中' if d_card else '未命中'}"
              f" | 识别 {r['rows']} 行"
              f" | 点难度 {w_pick} (期望 {r['want']}) {'OK' if d_row else '不符'}"
              f" | 点开始 {w_btn} (期望 {r['btn']}, score={r['btn_score']:.3f})"
              f" {'OK' if d_btn else '不符'}")
        if not ok:
            print("---- 日志 ----")
            for line in r["log"].splitlines():
                print("   ", line)

    print("\n—— 场景 B：地图卡片没加载也不影响流程"
          "（2026-11-18 19:46 起自动流程不再等「普通模式」卡片）——")
    for scale in (1.0, 0.63):
        r = run_once(scale, tmp, drop_card=True)
        w_pick = r["clicks"][0] if r["clicks"] else (None, None)
        w_btn = r["clicks"][1] if len(r["clicks"]) > 1 else (None, None)
        d_row = (abs(w_pick[0] - r["want"][0]) <= 3 and abs(w_pick[1] - r["want"][1]) <= 3) \
            if None not in r["want"] and None not in w_pick else False
        ok = d_row and len(r["clicks"]) >= 2
        ok_all = ok_all and ok
        print(f"{'✅' if ok else '❌'} 缩放 {scale:<5} 卡片区域已涂掉"
              f" | 仍点难度 {w_pick} {'OK' if d_row else '不符'}"
              f" | 仍点开始 {w_btn} {'OK' if len(r['clicks']) >= 2 else '缺失'}")
        if not ok:
            print("---- 日志 ----")
            for line in r["log"].splitlines():
                print("   ", line)

    print()
    print("全部通过 ✅" if ok_all else "有失败项 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
