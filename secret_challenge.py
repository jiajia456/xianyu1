# -*- coding: utf-8 -*-
"""
秘境挑战：海岛 / 不死 —— **按「勾选」执行**（用户 2026-11-16 13:20 需求）

用户原话：秘境挑战，到时我要写成软件，比如我要打海岛挑战打钩、或者不死挑战，
         要打就打钩，这个选项。

所以「打不打」不写死在代码里，而放在 configs/secret_challenge.json：

    options.island.checked = true    ← 海岛挑战打钩
    options.undead.checked = false   ← 不死挑战没勾

软件（将来不管用 Tkinter / Qt / 网页）只要改这个 json 再调本脚本即可。
命令行也能直接模拟勾选：--island / --undead（可同时），--save 写回 json。

现场坐标（客户区 1690x921，全部实测）：
  秘境挑战 NPC       (1181, 235)   点完面板切到「秘境挑战」
  海岛挑战（选项1）  (1360, 723)   → 弹窗「是否开启海岛秘境挑战?」
  不死挑战（选项2）  (1418, 723)   → 弹窗「是否攻打不死族?」
  弹窗「是」         (762, 466)    ⚠ 点了就传送，无法返回
  弹窗「否」         (919, 463)

两个弹窗的判据模板分开：
  dlg_island_title.png  278x24   自匹配 1.000 / 不死弹窗 0.695 / 无弹窗 0.402
  dlg_undead_title.png  360x75   自匹配 1.000 / 海岛弹窗 0.695 / 无弹窗 0.402
  （不死那个必须带两行说明一起裁：只裁标题行时在海岛弹窗上能拿 0.973，会误判）

默认行为：confirm_mode = "yes" —— **勾了就打（点「是」）**，这是实际应用的样子。
点「否」只是测试期用来抓坐标/验证定位的手段：命令行加 --no，或面板里勾「演练模式」。
（注意：点「是」后全员传送到秘境、无法返回，这是游戏原文提示。）

用法：
  python secret_challenge.py --check                  # 打印当前勾选
  python secret_challenge.py --island --dry           # 勾海岛，只打印计划（一下都不点）
  python secret_challenge.py --island                 # 勾海岛，真打（点「是」）
  python secret_challenge.py --undead                 # 勾不死，真打
  python secret_challenge.py --island --undead        # 两个都勾，依次打
  python secret_challenge.py --island --no            # 演练：弹窗点「否」，不真开
  python secret_challenge.py --island --save          # 把海岛打钩状态写回 json

实测规律：War3 所有 UI 的第一次点击必被吞（是/否/开始游戏/挑战存档都一样），
所以每次点确认后都要复查弹窗是否消失，最多重试 cfg.retry 次（默认 4）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

import cv2
import numpy as np

import game_auto as ga
import in_game as ig
import wave_auto as wv

CFG_PATH = "configs/secret_challenge.json"
KEYS = ("island", "undead", "delta")
TPL_NPC = "templates/npc_secret.png"
TPL_LBL = "templates/lbl_secret_npc.png"
# 最近一次定位命中的标签位置 (lx, ly, 模板说明)，给 locate_npc.py 画标记用
LAST_LABEL = None
# 最近一次定位产生的「可点击候选点」[(x, y, 来源说明), ...]，按可信度排好序
LAST_CANDS: list[tuple[int, int, str]] = []


def _win7() -> bool:
    """只看系统档位，W10 走原逻辑（2026-11-25 用户：W7 的改动不能影响 W10）"""
    try:
        import sysenv
        return bool(sysenv.is_win7())
    except Exception:
        return False


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 配置
def load_cfg(path: str = CFG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_cfg(cfg: dict, path: str = CFG_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def checked_keys(cfg: dict) -> list[str]:
    return [k for k in KEYS if cfg["options"][k].get("checked")]


# ---------------------------------------------------------------- 识别
def _tpl(path: str):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


_TPL_CACHE = {}


def _tpl_cached(name: str):
    """带缓存的模板读取（dialog_score 每次轮询都会调，别反复 imdecode）"""
    p = f"templates/{name}"
    if p not in _TPL_CACHE:
        _TPL_CACHE[p] = _tpl(p)
    return _TPL_CACHE[p]


# 弹窗只出现在画面中央。2026-11-26 23:10 用 W7 失败现场图实测：
#   不死弹窗按 1.00 匹配只有 0.618，把模板放大到 1.05 匹配是 0.899（阈值 0.85）。
#   也就是这条弹窗的渲染比模板大约 5%，只按 1.00 单尺度匹配会永远判「弹窗没出现」，
#   于是 click_confirm 一次都不会跑（22:53 那轮就是这么空转 55.7s 后失败的）。
# 所以改成中心区域小范围多尺度：先 1.00 / 1.05 两档（约 100ms），
# 最高分到 0.80 才补 1.02~1.06 细扫（只有疑似命中时才多花约 150ms）。
DLG_ROI = (0.18, 0.10, 0.82, 0.78)          # 左 / 上 / 右 / 下（按画面比例）
DLG_SCALES = (1.00, 1.05)
DLG_SCALES_FINE = (1.02, 1.03, 1.04, 1.06)
DLG_LAST_SCALE = 1.0                        # 最近一次得分命中的缩放档（只用于打日志）


def _dlg_match(roi, tpl, scales):
    """在 roi 里按若干缩放档匹配，返回 (最高分, 对应缩放档)"""
    best, best_sc = -1.0, 1.0
    for sc in scales:
        if abs(sc - 1.0) < 1e-6:
            t = tpl
        else:
            t = cv2.resize(tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        if t.shape[0] >= roi.shape[0] or t.shape[1] >= roi.shape[1]:
            continue
        s = float(cv2.minMaxLoc(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED))[1])
        if s > best:
            best, best_sc = s, sc
    return best, best_sc


def dialog_score(img, opt: dict) -> float:
    """当前画面里这个挑战的确认弹窗有多像（0~1）

    只在画面中央找（弹窗位置固定），并按 1.00 / 1.05 两档缩放匹配 ——
    模板和实机渲染差 5% 时 1.00 只有 0.618，1.05 是 0.899，单尺度会漏判。
    最高分 ≥0.80 时再补细档，把分数做稳。
    """
    global DLG_LAST_SCALE
    tpl = _tpl_cached(opt["dialog_tpl"])
    if tpl is None:
        return -1.0
    h, w = img.shape[:2]
    x0, y0, x1, y1 = DLG_ROI
    roi = img[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
    if roi.size == 0:
        roi = img
    best, sc = _dlg_match(roi, tpl, DLG_SCALES)
    if best >= 0.80:
        s2, sc2 = _dlg_match(roi, tpl, DLG_SCALES_FINE)
        if s2 > best:
            best, sc = s2, sc2
    DLG_LAST_SCALE = sc
    return best


def which_dialog(img, cfg: dict):
    """返回 (key, score)：画面上的弹窗属于哪个挑战；都没有返回 (None, 最高分)"""
    best, best_s = None, -1.0
    for k in KEYS:
        o = cfg["options"][k]
        s = dialog_score(img, o)
        if s >= o["dialog_score"] and s > best_s:
            best, best_s = k, s
        elif s > best_s:
            best_s = s
    return best, best_s


# ------------------------------------------------ 「成功对话」判据：面板两格图标
ICON_TPL = {"island": "templates/ic_island.png", "undead": "templates/ic_undead.png",
            "delta": "templates/ic_delta.png"}
_ICON_CACHE: dict[str, "object"] = {}


def icon_score(img, key: str, scales=None, roi_y: float = 0.66):
    """底部面板里找某个格子图标（多尺度），返回 (score, (cx,cy), scale)。

    模板来自用户 2026-11-16 15:25 给的面板截图（340×223，纵向比例 1:1，横向步长 76）。
    只在画面下 34% 找，避免和世界里的建筑混淆。
    尺度范围放宽到 0.6~1.8 —— 用户会伸缩窗口，图标像素尺寸会跟着变。
    """
    if key not in _ICON_CACHE:
        tpl = _tpl(ICON_TPL[key])
        if tpl is None:
            return 0.0, None, 1.0
        _ICON_CACHE[key] = tpl
    tpl0 = _ICON_CACHE[key]
    if scales is None:
        scales = [round(0.6 + 0.05 * i, 2) for i in range(25)]   # 0.60 ~ 1.80
    y0 = int(img.shape[0] * roi_y)
    roi = img[y0:, :]
    best = (0.0, None, 1.0)
    for sc in scales:
        t = tpl0 if abs(sc - 1.0) < 1e-6 else cv2.resize(
            tpl0, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        if t.shape[0] >= roi.shape[0] or t.shape[1] >= roi.shape[1]:
            continue
        res = cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, ml = cv2.minMaxLoc(res)
        if mx > best[0]:
            best = (float(mx), (ml[0] + t.shape[1] // 2, ml[1] + t.shape[0] // 2 + y0), sc)
    return best


def panel_secret_open(img, cfg: dict | None = None, threshold: float | None = None,
                      extra_keys=()):
    """判定「秘境挑战」面板是否已打开 —— 也就是「成功找到 NPC 并成功对话」。

    判据：底部面板第 1、2 格同时出现海岛格图标和不死族图标（用户 15:25 截图定义）。
    extra_keys：额外要打分的格子（如 "delta" 三角洲），只补进 info 供定位点击用，
                **不参与** ok 判定 —— 面板开没开仍只看前两格。
    返回 (ok, info)  info = {'island': (score,pos), 'undead': (score,pos), ...}
    """
    th = threshold if threshold is not None else float(
        (cfg or {}).get("panel_icon_score", 0.62))
    # 截图已统一归一化到 1690x921（in_game.grab），图标模板也是这个尺寸裁的，
    # 所以只需要 1.0 附近几档；旧版 0.6~1.8 共 25 档，一次判定要多花 2 秒。
    sc_ = (cfg or {}).get("icon_scales") or [round(0.85 + 0.05 * i, 2) for i in range(7)]
    isl = icon_score(img, "island", scales=sc_)
    und = icon_score(img, "undead", scales=sc_)
    info = {"island": isl, "undead": und, "threshold": th}
    for k in extra_keys:
        if k not in info and k in ICON_TPL:
            info[k] = icon_score(img, k, scales=sc_)
    ok = isl[0] >= th and und[0] >= th
    return ok, info



def wait_dialog(w, cfg: dict, key: str, timeout: float):
    """等某个挑战的弹窗出现。返回 (出现?, 分数)"""
    opt = cfg["options"][key]
    t0 = time.time()
    s = -1.0
    while time.time() - t0 < timeout:
        img = ig.grab_checked(w)          # 顺带查作弊提示
        s = dialog_score(img, opt)
        if s >= opt["dialog_score"]:
            return True, s
        time.sleep(0.35)
    return False, s


def _ring_mask(img, lc: dict):
    """青色光圈的像素掩码：BGR 直判 + HSV 色相兜底。

    11-16 定的是 (b>140)&(g>130)&(r<120)。2026-11-18 22:00 那一帧一个连通块
    都没过，怀疑是光照/时段让青色偏了，所以再加一路 HSV：色相落在青绿区间
    (80~105/179) 且饱和度、亮度够，两条任一成立即算。只放宽「是不是青色」，
    尺寸/形状判据不动。
    """
    b = img[:, :, 0].astype(int)
    g = img[:, :, 1].astype(int)
    r = img[:, :, 2].astype(int)
    m = (b > 140) & (g > 130) & (r < 120)
    if lc.get("ring_hsv", True):
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        h = hsv[:, :, 0].astype(int)
        sa = hsv[:, :, 1].astype(int)
        va = hsv[:, :, 2].astype(int)
        lo, hi = int(lc.get("ring_h_lo", 80)), int(lc.get("ring_h_hi", 105))
        m = m | ((h >= lo) & (h <= hi)
                 & (sa > int(lc.get("ring_s_min", 90)))
                 & (va > int(lc.get("ring_v_min", 150))))
        pm = _pink_mask(img, lc)
        if pm is not None:
            m = m | (pm > 0)
    return m.astype(np.uint8)


def _pink_mask(img, lc: dict):
    """品红/粉色光圈掩码 —— 只在 Win7 开（W10 原逻辑不动）。

    2026-11-25 W7 现场（日志 secret_frame_npc_180524.png）：「秘境挑战」NPC 脚下的圈
    是品红，实测 HSV≈(150-160, 88, 241)、BGR≈(224,157,241)，青色通道一个像素都过不了，
    结果 0 个主候选 → 退化「按标签偏移盲点」，点了 8 次才中。
    """
    if not (_win7() and lc.get("ring_pink_win7", True)):
        return None
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h = hsv[:, :, 0].astype(int)
    sa = hsv[:, :, 1].astype(int)
    va = hsv[:, :, 2].astype(int)
    plo, phi = int(lc.get("ring_pink_h_lo", 145)), int(lc.get("ring_pink_h_hi", 168))
    return (((h >= plo) & (h <= phi)
             & (sa > int(lc.get("ring_pink_s_min", 70)))
             & (va > int(lc.get("ring_pink_v_min", 150)))).astype(np.uint8))


def _circle_cands(img, lx: int, ly: int, lc: dict, scale: float = 1.0,
                  limit: int = 6) -> list:
    """标签下方找青色光圈，返回**排好序的候选列表**（旧版只给一个点）。

    为什么要给列表：光圈不保证每次都在（可能是脉冲动画 / 被建筑挡住 / 镜头角度变），
    这种时候硬算一个点出来就是点空。候选按「横向对齐优先」排序：

        d = |cx - 期望x| + 0.15 * |cy - 期望y|

    横向才是区分「秘境 NPC / 并排的另一个 NPC」的关键；纵向随镜头俯仰 70~160 变，
    只给 0.15 权重不能主导选择（15:44 实测：标签 y=93 光圈 y=251，dy=158）。

    tier=0：面积达标（circle_area，11-16 实测真光圈 1600+）→ 可直接点
    tier=1：面积偏小（circle_area_min 起）→ 只当备选，最后兜底才点
    返回 [{'x','y','area','d','tier','w','h','dx','dy'}, ...]，按 (tier, d) 升序。
    """
    s = float(scale)
    mask = _ring_mask(img, lc)
    win_x, win_y = int(round(140 * s)), int(round(230 * s))
    x1, y1 = max(0, lx - win_x), max(0, ly + int(round(10 * s)))
    x2, y2 = min(img.shape[1], lx + win_x), min(img.shape[0], ly + win_y)
    if x2 <= x1 or y2 <= y1:
        return []
    sub = mask[y1:y2, x1:x2]
    n, lab, st, ce = cv2.connectedComponentsWithStats(sub, 8)
    pink = _pink_mask(img, lc)
    sub_pink = None if pink is None else pink[y1:y2, x1:x2]
    exp_x = lx - int(round(int(lc.get("circle_dx", 18)) * s))
    exp_y = ly + int(round(int(lc.get("circle_dy", 70)) * s))
    a_hi = int(round(int(lc.get("circle_area", 900)) * s * s))
    a_lo = int(round(int(lc.get("circle_area_min", 300)) * s * s))
    min_bw = int(round(int(lc.get("circle_min_w", 26)) * s))
    min_bh = int(round(int(lc.get("circle_min_h", 14)) * s))
    # max_dx 现在只是「防呆上限」，不再用来硬筛：横向前 55px 那个门槛把真光圈
    # 也一起挡掉了（22:00 那次），排序交给 d，筛不筛由面板判据说了算。
    max_dx = float(lc.get("circle_max_dx", 140)) * s
    max_dy = float(lc.get("circle_max_dy", 240)) * s
    ar_lo = float(lc.get("circle_aspect_lo", 1.05))
    ar_hi = float(lc.get("circle_aspect_hi", 3.2))
    out = []
    for i in range(1, n):
        area = int(st[i, 4])
        bw, bh = int(st[i, 2]), int(st[i, 3])
        if area < a_lo or bw < min_bw or bh < min_bh:
            continue
        ar = bw / float(max(1, bh))
        # 品红圈被 NPC 本体挡掉一半，bbox 接近正方形（实测 26x26，ratio=1.00），
        # 用青圈的 1.05 下限会把它整条丢掉 → 品红候选单独放宽
        is_pink = False
        if sub_pink is not None:
            comp = (lab == i)
            pink_n = int(sub_pink[comp].sum())
            is_pink = pink_n >= max(60, int(0.45 * area))
        if ar < (float(lc.get("circle_aspect_lo_pink", 0.8)) if is_pink else ar_lo) \
                or ar > ar_hi:            # 光圈是扁椭圆；竖长的青色是披风/护甲
            continue
        cx, cy = x1 + int(ce[i][0]), y1 + int(ce[i][1])
        ddx, ddy = abs(cx - exp_x), abs(cy - exp_y)
        if ddx > max_dx or ddy > max_dy:
            continue
        # 品红圈面积天生偏小（被 NPC 挡住），只要过了 area_min 就算主候选
        out.append({"x": cx, "y": cy, "area": area, "w": bw, "h": bh,
                    "dx": ddx, "dy": ddy, "d": ddx + 0.15 * ddy,
                    "tier": 0 if (area >= a_hi or is_pink) else 1,
                    "pink": is_pink})
    out.sort(key=lambda c: (c["tier"], c["d"]))
    return out[:limit]


def _circle_below(img, lx: int, ly: int, lc: dict, scale: float = 1.0):
    """兼容旧调用 / 自检：只返回 tier=0 的最佳候选 (cx, cy, area)，没有就 None。"""
    for c in _circle_cands(img, lx, ly, lc, scale=scale):
        if c["tier"] == 0:
            return c["x"], c["y"], c["area"]
    return None


def _mask_roi(res, lc: dict, shape, key: str = "label_roi_y"):
    """把模板匹配结果图里「UI 区域」的行整片打成 -1，避免面板标题抢最高分。

    2026-11-16 15:43 踩到的坑：底部存档挑战面板开着时，页面标题「秘境挑战」
    和世界里 NPC 头顶的标签是同一套字，模板匹配给了它 0.949（真 NPC 反而没它高），
    于是定位点落到 UI 空白处、光圈自然验不出来。用 ROI 把底部面板切掉即可。
    """
    roi = lc.get(key)
    if not roi:
        return res
    h = int(shape[0])                      # 按原图高度算，模板高度不同也不跑偏
    y0, y1 = int(round(float(roi[0]) * h)), int(round(float(roi[1]) * h))
    y1 = min(y1, res.shape[0])
    y0 = max(0, min(y0, res.shape[0]))
    out = res
    if y0 > 0:
        out[:y0, :] = -1.0
    if y1 < h:
        out[y1:, :] = -1.0
    return out


def _top_peaks(res, k: int, thr: float, rad_x: int, rad_y: int):
    """取结果图里前 k 个互相不挨着的峰，返回 [(score, (x,y)), ...]（分数降序）。"""
    peaks = []
    rr = res.copy()
    for _ in range(max(1, k)):
        _, v, _, p = cv2.minMaxLoc(rr)
        if v < thr:
            break
        peaks.append((float(v), p))
        px, py = p
        rr[max(0, py - rad_y):py + rad_y + 1,
           max(0, px - rad_x):px + rad_x + 1] = -1.0
    return peaks


def _label_channel(img, lc: dict, tpl_path: str, thr: float, tag: str, verbose: bool = True):
    """单个文字模板的完整定位流程：多尺度找最高分 → 前 N 个候选逐个验光圈。

    返回值：
      ((cx,cy,score,scale), "hit")     找到且光圈校验通过
      ((cx,cy,score,scale), "guess")   只有文字没光圈，按偏移推算（不完美但可用）
      (None, "low")                    分数不够，模板不可用
    """
    global LAST_LABEL
    lbl = _tpl(tpl_path)
    if lbl is None:
        return None, "low"

    lo, hi, step = lc.get("label_scales", [0.7, 1.3, 0.1])
    mx, sc_best = 0.0, 1.0
    sc = float(lo)
    while sc <= float(hi) + 1e-6:
        w_, h_ = int(round(lbl.shape[1] * sc)), int(round(lbl.shape[0] * sc))
        if 8 <= w_ < img.shape[1] and 8 <= h_ < img.shape[0]:
            t = lbl if abs(sc - 1.0) < 1e-6 else cv2.resize(
                lbl, (w_, h_), interpolation=cv2.INTER_AREA)
            r = _mask_roi(cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED), lc, img.shape)
            _, v, _, _ = cv2.minMaxLoc(r)
            if v > mx:
                mx, sc_best = float(v), sc
        sc += float(step)

    if mx < thr:
        if verbose:
            log(f"   ℹ {tag}文字通道 {mx:.3f} < {thr}")
        return None, "low"

    # 同尺寸重算结果图，取前 N 个候选逐个验光圈：
    # 底部面板标题「秘境挑战」和世界里的 NPC 标签是同一套字，单取最高分会误判到 UI 上。
    t = lbl if abs(sc_best - 1.0) < 1e-6 else cv2.resize(
        lbl, (int(round(lbl.shape[1] * sc_best)), int(round(lbl.shape[0] * sc_best))),
        interpolation=cv2.INTER_AREA)
    r = _mask_roi(cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED), lc, img.shape)
    peaks = _top_peaks(r, int(lc.get("npc_candidates", 4)), thr,
                       max(6, t.shape[1] // 2), max(4, t.shape[0]))
    if verbose and peaks:
        log("   · " + tag + "候选：" + " / ".join(
            "%.3f@(%d,%d)" % (v, p[0] + t.shape[1] // 2, p[1] + t.shape[0] // 2)
            for v, p in peaks))
    primary, guess = None, None
    for i, (v, p) in enumerate(peaks):
        lx = p[0] + t.shape[1] // 2
        ly = p[1] + t.shape[0] // 2
        cands = _circle_cands(img, lx, ly, lc, scale=sc_best)
        for c in cands:
            LAST_CANDS.append((c["x"], c["y"],
                               "%s光圈%s%dpx" % (tag, "主" if c["tier"] == 0 else "备",
                                                 c["area"])))
        if primary is None:
            best0 = None
            for c in cands:
                if c["tier"] == 0:
                    best0 = c
                    break
            if best0:
                primary = (best0, lx, ly, float(v), i + 1)
        if guess is None:
            guess = (v, lx, ly)
    if primary:
        c, lx, ly, v, idx = primary
        LAST_LABEL = (lx, ly, tag)
        if verbose:
            log("   🔍 %s标签@(%d,%d) → 光圈@(%d,%d) score=%.3f scale=%.2f 青=%d "
                "候选%d个/第%d" % (tag, lx, ly, c["x"], c["y"], v, sc_best,
                                  c["area"], len(peaks), idx))
        return (c["x"], c["y"], v, sc_best, lx, ly), "hit"
    if guess:
        v, lx, ly = guess
        cx = lx - int(round(int(lc.get("circle_dx", 18)) * sc_best))
        cy = ly + int(round(int(lc.get("circle_dy", 70)) * sc_best))
        LAST_LABEL = (lx, ly, tag)
        if verbose:
            log("   ⚠ %s标签@%d,%d score=%.3f：%d 个候选都没验出光圈，按偏移推算 "
                "(%d,%d)" % (tag, lx, ly, v, len(peaks), cx, cy))
        return (cx, cy, float(v), sc_best, lx, ly), "guess"
    return None, "low"


def _model_channel(img, lc: dict, roi=None, verbose: bool = True):
    """NPC 模型多尺度模板匹配 + 青色像素校验。

    npc_secret.png（100x76）是从截图裁的**带脚下光圈**的整只 NPC，所以这条路
    一旦命中，位置本身就是「NPC + 光圈」的位置，比「标签 + 推算」准得多。
    roi=(x1,y1,x2,y2) 时只在标签附近找，快 20 倍；None 才全屏扫（兜底）。
    """
    tpl0 = _tpl(lc.get("tpl", TPL_NPC))
    if tpl0 is None:
        return None
    h0, w0 = tpl0.shape[:2]
    H, W = img.shape[:2]
    if roi:
        x1, y1, x2, y2 = [int(v) for v in roi]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(W, x2), min(H, y2)
    else:
        x1, y1, x2, y2 = 0, 0, W, H
    if x2 - x1 < 60 or y2 - y1 < 60:
        return None
    sub = img[y1:y2, x1:x2]
    if roi:
        lo, hi = lc.get("scale_range_roi", [0.85, 1.2])
        min_score = float(lc.get("min_score_roi", 0.78))
    else:
        lo, hi = lc.get("scale_range", [0.65, 1.45])
        min_score = float(lc.get("min_score", 0.72))
    min_cyan = int(lc.get("cyan_min", 700))

    best = (0.0, 0, 0, 1.0)
    sc = float(lo)
    while sc <= float(hi) + 1e-6:
        w, h = int(round(w0 * sc)), int(round(h0 * sc))
        if 10 <= w < sub.shape[1] and 10 <= h < sub.shape[0]:
            tpl = cv2.resize(tpl0, (w, h), interpolation=cv2.INTER_AREA)
            _, mx, _, ml = cv2.minMaxLoc(
                cv2.matchTemplate(sub, tpl, cv2.TM_CCOEFF_NORMED))
            if mx > best[0]:
                best = (float(mx), x1 + ml[0] + w // 2, y1 + ml[1] + h // 2, sc)
        sc += 0.05

    score, cx, cy, sc = best
    if score < min_score:
        if verbose:
            log("   ⚠ NPC 模型通道没过阈值：最高 %.3f（要 %.2f）%s"
                % (score, min_score, "（标签附近）" if roi else "（全屏）"))
        return None
    b = img[:, :, 0].astype(int)
    g = img[:, :, 1].astype(int)
    r = img[:, :, 2].astype(int)
    cyan = (b > 150) & (g > 140) & (r < 110)
    n_cyan = int(cyan[max(0, cy - 45):cy + 45, max(0, cx - 45):cx + 45].sum())
    if n_cyan < min_cyan:
        if verbose:
            log("   ⚠ NPC 模型像但脚下青色不够（%d < %d）→ 不认" % (n_cyan, min_cyan))
        return None
    if verbose:
        log("   🔍 NPC 模型通道命中 @(%d,%d) score=%.3f scale=%.2f 青=%d"
            % (cx, cy, score, sc, n_cyan))
    return cx, cy, score, sc


def locate_npc(img, cfg: dict | None = None, verbose: bool = True):
    """图色定位「秘境挑战」NPC —— 两通道都跑，谁「验过」用谁。

    通道 1：头顶「秘境挑战」白字黑边标签（82x21）→ 标签下方青色光圈二次定位；
    通道 2：NPC 模型模板（100x76，自带脚下光圈）+ 青色像素校验。

    2026-11-18 22:10 改：旧版「标签通道只要返回了结果（哪怕是 guess）就直接
    return，模型通道根本不跑」，而 guess 恰恰是最不可靠的 —— 22:00 那次就是
    这样连点 3 下、画面差分 0.000 全被吞。现在标签 guess 只当备选，模型通道
    照样在标签附近找一遍，验过优先。

    实测区分度（2026-11-16 13:59）：秘境 NPC 模板自匹配 0.881 / 存档 NPC 处 0.148；
    青色像素：秘境 1680 / 存档 514 → cyan_min 700。
    返回 (x, y, score, scale) 或 None。
    """
    global LAST_CANDS, LAST_LABEL
    lc = (cfg or {}).get("npc_locate", {})
    LAST_CANDS = []
    LAST_LABEL = None

    guess, label_xy = None, None
    for tag, path, thr_key in (
        ("四字", lc.get("label_tpl", TPL_LBL), "label_score"),
        ("两字", lc.get("label_tpl_short"), "label_score_short"),
    ):
        if not path:
            continue
        thr = float(lc.get(thr_key, 0.70))
        res, kind = _label_channel(img, lc, path, thr, tag, verbose)
        if res is None:
            continue
        label_xy = (res[4], res[5])
        if kind == "hit":
            if verbose:
                log("   ✔ 文字+光圈通道已验证 → 直接用")
            return res[:4]
        if guess is None:
            guess = res

    roi = None
    if label_xy:
        px = int(lc.get("model_pad_x", 210))
        py = int(lc.get("model_pad_y", 340))
        roi = (label_xy[0] - px, label_xy[1] - 50, label_xy[0] + px, label_xy[1] + py)
    m = _model_channel(img, lc, roi=roi, verbose=verbose)
    if m:
        LAST_CANDS.insert(0, (m[0], m[1], "NPC模型"))
        if verbose and guess:
            log("   ✔ 模型通道比分更高（%.3f > %.3f）→ 用模型点"
                % (m[2], guess[2]))
            LAST_LABEL = (label_xy[0], label_xy[1], "标签(仅参考)")
        return m
    if guess:
        LAST_LABEL = (guess[4], guess[5], "标签")
        if verbose:
            log("   ℹ 只有标签推算点可用（会先单击试，不行再换候选）")
        return guess[:4]
    return None


# 配置里的坐标都是按 1690x921 客户区标定的；用户伸缩窗口后要按比例换算
REF_SIZE = (1690, 921)


def _scale_xy(w, xy):
    """标定坐标 → 点击坐标。

    2026-11-17 起，in_game.grab() 把截图统一归一化到 1690x921，inp 又带着
    ref_size 在点击时放大回实际客户区 —— 所以这里**不能再乘一次**（否则双重换算，
    小窗口下会把鼠标点到右下角）。保留函数只为兼容旧调用点。
    """
    return int(xy[0]), int(xy[1])


# ---------------------------------------------------------------- 动作
def prepare_view(w, inp, cfg: dict, verbose: bool = True):
    """开面板前的标准镜头准备：**滚轮下滚 8 格拉远 → 按 F1**。

    用户 2026-11-16 16:03 定案的标准流程。为什么必须拉远：
    镜头近的时候秘境 NPC 贴着右侧玩家列表面板，「挑战」两字被压住，
    四字标签模板只有 0.54~0.68，只能退到两字模板凑合（还容易点偏）。
    滚轮下滚 8 格（delta=-120）拉远后，四字模板稳定 0.97~0.99。
    反过来上滚是拉近，滚过头 NPC 会挤出画面，F1 也找不回来。

    配置：zoom_out_times（默认 8，0 = 不滚）、zoom_delta（默认 -120，负数=拉远）、
          press_f1（默认 True）。
    """
    times = int(cfg.get("zoom_out_times", 8))
    if _win7():
        # 2026-11-27 用户定：W7 这台拉远 2 格（8 格会把 NPC 缩太小，品红圈更碎）。
        # 只影响 Win7；W10 仍走 zoom_out_times=8。
        times = int(cfg.get("zoom_out_times_win7", 2))
    if times > 0:
        try:
            # 用归一化坐标（1690x921 系）算落点：inp 带 ref_size，会把坐标按实际
            # 客户区放大回去。2026-11-17 起截图统一归一化，这里不能再传真实像素。
            px, py = REF_SIZE[0] // 2, REF_SIZE[1] // 2 - 80
            inp.wheel(px, py, times=times,
                      delta=int(cfg.get("zoom_delta", -120)),
                      gap=float(cfg.get("zoom_gap", 0.25)))
            if verbose:
                log(f"   🔭 滚轮拉远 {times} 格" + ("（Win7 档）" if _win7() else ""))
            time.sleep(float(cfg.get("zoom_settle", 1.0)))
        except Exception as e:
            if verbose:
                log(f"   ⚠ 滚轮拉远失败（{e}），继续")
    if cfg.get("press_f1", True):
        inp.key("f1")
        time.sleep(0.45)


def _click(inp, cfg: dict, x, y, hover: float | None = None,
           clicks: int | None = None):
    """统一点击入口：默认双击（2 下，间隔 cfg["click_gap"]）。

    2026-11-18 20:10 用户要求：秘境挑战的点击也统一成双击（与提前挑战 / 挑战存档一致）。
    War3 偶尔会吞掉单次单击，双击里的第二下往往就生效；外面本来就各有重试
    （NPC 换 5 个偏移点、格子原地 2 次、确认 retry 4 次），双击只是让「一次尝试」
    更结实，重试结构没有变。
    """
    n = max(1, int(clicks if clicks is not None else cfg.get("click_clicks", 2)))
    gap = float(cfg.get("click_gap", 0.12))
    if hover is None:
        inp.click(x, y, hold=cfg["click_hold"], clicks=n, gap=gap)
    else:
        inp.click_hover(x, y, hover=hover, hold=cfg["click_hold"],
                        clicks=n, gap=gap)


def _uniq_path(d: str, prefix: str) -> str:
    """同一秒里连续存两张图时别撞名 —— 宿主机对「已存在的文件」拒绝覆盖（Errno 13）。"""
    base = os.path.join(d, "%s_%s" % (prefix, time.strftime("%H%M%S")))
    p = base + ".png"
    n = 1
    while os.path.exists(p):
        p = "%s_%d.png" % (base, n)
        n += 1
    return p


def dump_fail(img, click_xy, cfg: dict):
    """面板没打开时把画面存下来，并标出「标签点 / 正确期望点 / 实际点击点」。

    2026-11-18 21:31 这次（海岛秘境）点了 4 次都没开面板，日志只有坐标：
    标签@(1120,203)、光圈@(1013,274) —— 而 11-16 实测真光圈在 (1102,272)。
    差 89px，说明这一帧没认出真光圈、抓到了旁边另一小团青色。没有截图就只能猜，
    所以失败时统一落一张带标记的图，下次直接看图定位。
    """
    d = os.path.join("logs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    path = _uniq_path(d, "secret_npc_fail")
    vis = img.copy()
    lc = (cfg or {}).get("npc_locate", {})
    if LAST_LABEL:
        lx, ly, _tag = LAST_LABEL
        ex = lx - int(lc.get("circle_dx", 18))
        ey = ly + int(lc.get("circle_dy", 70))
        cv2.drawMarker(vis, (lx, ly), (0, 255, 255), cv2.MARKER_CROSS, 26, 2)   # 黄=标签
        cv2.drawMarker(vis, (ex, ey), (0, 255, 0), cv2.MARKER_TILTED_CROSS, 22, 2)  # 绿=期望光圈
    if click_xy:
        cv2.drawMarker(vis, (int(click_xy[0]), int(click_xy[1])),
                       (0, 0, 255), cv2.MARKER_CROSS, 30, 3)                      # 红=最后点击
    cv2.putText(vis, "red=last click   yellow=label   green=expected ring",
                (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 1, cv2.LINE_AA)
    cv2.imencode(".png", vis)[1].tofile(path)
    log(f"   📷 诊断图已存：{os.path.abspath(path)}")
    return path


def _frame_diff(a, b) -> float:
    """两张图差多少（0~1）。用来区分「点了没反应」和「画面已经变了」。"""
    try:
        if a is None or b is None or a.shape != b.shape:
            return -1.0
        x = cv2.resize(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), (340, 185))
        y = cv2.resize(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), (340, 185))
        return float(np.abs(x.astype(np.int16) - y.astype(np.int16)).mean()) / 255.0
    except Exception:
        return -1.0


_RM_FAILED = set()          # 本进程内 os.remove 失败过的文件，不再反复删


def dump_frame(img, tag: str, queue=None, keep: int = 4):
    """把当前帧落盘（带标记），只留最近 keep 张。

    2026-11-18 22:00 那次失败一张图都没留下（dump_fail 要等候选全试完才调），
    日志包里只剩坐标，根本没法判断点在了什么上面。现在一进 open_panel 就存一张，
    日志收集器会把 logs 根目录的 png 自动收进包里。
    """
    d = "logs"
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    try:
        olds = sorted(glob.glob(os.path.join(d, "secret_frame_*.png")))
        # 2026-11-19 20:56 用户定：宿主机上删不掉的文件记进 _RM_FAILED 后不再反复
        # os.remove —— 每次调用宿主文件代理都会刷一行 SAFE_DELETE_FAIL_CLOSED，
        # 旧图堆积无害（几百 KB），刷屏才有害。保留 keep=4 的滑动窗口语义不变。
        todo = [f for f in olds[:max(0, len(olds) - keep + 1)]
                if f not in _RM_FAILED]
        for f in todo:
            try:
                os.remove(f)
            except Exception:
                _RM_FAILED.add(f)
    except Exception:
        pass
    path = _uniq_path(d, "secret_frame_%s" % tag)
    vis = img.copy()
    if LAST_LABEL:
        cv2.drawMarker(vis, (LAST_LABEL[0], LAST_LABEL[1]),
                       (0, 255, 255), cv2.MARKER_CROSS, 26, 2)
    for i, (x, y, _src) in enumerate((queue or [])[:12], 1):
        cv2.drawMarker(vis, (int(x), int(y)), (0, 255, 0),
                       cv2.MARKER_TILTED_CROSS, 16, 1)
        cv2.putText(vis, str(i), (int(x) + 6, int(y) - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1, cv2.LINE_AA)
    cv2.putText(vis, "yellow=label  green=click candidates (1..n)",
                (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1, cv2.LINE_AA)
    try:
        cv2.imencode(".png", vis)[1].tofile(path)
        log("   📷 画面已存：%s" % os.path.abspath(path))
    except Exception as e:
        log("   ⚠ 画面保存失败：%s" % e)
        return ""
    return path


def _click_queue(loc, cfg: dict):
    """列出「可以试着点」的点，按可信度排序，去重后返回 [(x, y, 来源)]。

    为什么给列表：面板开没开是个**可靠的裁判**（两格图标同时出现才算成功对话）。
    与其赌一个算出来的点，不如按可信度依次试，试到裁判点头为止。
    """
    lc = cfg.get("npc_locate", {})
    q = []

    def add(x, y, src):
        x, y = int(round(x)), int(round(y))
        if x <= 0 or y <= 0:
            return
        for px, py, _s in q:
            if abs(px - x) <= 8 and abs(py - y) <= 8:
                return
        q.append((x, y, src))

    if loc:
        add(loc[0], loc[1], "定位")
    for x, y, src in LAST_CANDS:
        add(x, y, src)
    if LAST_LABEL:
        lx, ly = int(LAST_LABEL[0]), int(LAST_LABEL[1])
        dx0 = int(lc.get("circle_dx", 18))
        dy0 = int(lc.get("circle_dy", 70))
        for dy in (dy0, dy0 + 20, dy0 - 18, dy0 + 45, dy0 + 80, dy0 + 120):
            for dx in (-dx0, -dx0 - 22, -dx0 + 22, 0):
                add(lx + dx, ly + dy, "标签+(%d,%d)" % (dx, dy))
    try:
        add(cfg["npc_xy"][0], cfg["npc_xy"][1], "配置坐标")
    except Exception:
        pass
    return q


def open_panel(w, inp, cfg: dict, verbose: bool = True):
    """点秘境 NPC 打开选择面板，并用「两格图标」判定是否真的成功对话。

    用户 2026-11-16 14:23 提示：看不到「秘境挑战」字体时按一下 F1 就能显示出来。
    用户 16:03 定案：**下滚 8 格拉远 → F1 → 再定位**，见 prepare_view()。
    用户 15:28 提示：要定位文字**下面的光圈**，点光圈才准 —— 见 _circle_cands()。

    2026-11-18 22:10 改（那次连点 3 下、画面差分 0.000，面板一直 0.47/0.46）：
      · 定位只做一次，产出「候选点击列表」；每点一下都用面板判据当裁判，
        不行就换下一个候选（光圈主选 → 光圈备选 → 标签网格 → 配置坐标）。
      · 第 1 下用**单击**：War3 里对 NPC 双击有概率「刚开的面板又被关掉」，
        而面板判据只看最终状态，那就会一直判失败。单击不行后面再按双击。
      · 每下都记「画面差分」，≈0 表示这一下被完全吞掉（死点/被挡住）。

    返回 (x, y, ok)：ok=True 表示面板已切到秘境页、两格图标都在。
    """
    img0 = ig.grab_checked(w)
    ok0, info0 = panel_secret_open(img0, cfg)
    if ok0:
        if verbose:
            log("   ✅ 面板已经开着（海岛格 %.2f / 不死格 %.2f），跳过镜头准备"
                % (info0["island"][0], info0["undead"][0]))
        return 0, 0, True

    prepare_view(w, inp, cfg, verbose=verbose)

    img = ig.grab_checked(w)
    ok0, info0 = panel_secret_open(img, cfg)
    if ok0:
        if verbose:
            log("   ✅ 拉远后一抓面板就开着（海岛格 %.2f）" % info0["island"][0])
        return 0, 0, True

    loc = locate_npc(img, cfg, verbose=verbose)
    if loc is None and verbose:
        log("   ℹ 标签 / 模型都没过阈值 → 只能靠配置坐标")
    queue = _click_queue(loc, cfg)
    if not queue:
        if verbose:
            log("   ❌ 连一个候选点都算不出来")
        return 0, 0, False
    if verbose:
        show = " ".join("%d:(%d,%d)[%s]" % (i, x, y, src)
                        for i, (x, y, src) in enumerate(queue[:8], 1))
        log("   🎯 候选点 %d 个：%s%s"
            % (len(queue), show, " …" if len(queue) > 8 else ""))
    try:
        dump_frame(img, "npc", queue)
    except Exception:
        pass

    max_tries = min(len(queue), int(cfg.get("max_click_tries", 16)))
    last, prev = (0, 0), img
    for i, (x, y, src) in enumerate(queue[:max_tries], 1):
        clicks = 1 if i == 1 else max(1, int(cfg.get("click_clicks", 2)))
        _click(inp, cfg, x, y, hover=cfg.get("npc_hover", 0.5), clicks=clicks)
        time.sleep(cfg["panel_settle"])
        img = ig.grab_checked(w)
        ok, info = panel_secret_open(img, cfg)
        diff = _frame_diff(prev, img)
        last = (x, y)
        if verbose:
            note = "（差分≈0，这一下被吞了）" if 0 <= diff < 0.002 else ""
            log("   第 %d 次点 (%d,%d) [%s] %d 下 → 海岛格 %.2f 不死格 %.2f "
                "差分 %.3f%s%s"
                % (i, x, y, src, clicks, info["island"][0], info["undead"][0],
                   diff, note, "  ✅ 面板已开" if ok else ""))
        if ok:
            return x, y, True
        prev = img
    if verbose:
        log("   ❌ 试了 %d 个候选点面板都没切到秘境页（两格图标未同时出现）"
            % max_tries)
    try:
        dump_fail(img, last, cfg)
    except Exception as e:
        log("   ⚠ 诊断图保存失败：%s" % e)
    return last[0], last[1], False


def click_confirm(w, inp, cfg: dict, key: str, confirm: str):
    """点「是」/「否」，直到弹窗消失。返回点了几次；None = 一直没关掉。"""
    opt = cfg["options"][key]
    target = cfg["yes_xy"] if confirm == "yes" else cfg["no_xy"]
    for n in range(1, cfg["retry"] + 1):
        _click(inp, cfg, *target)
        time.sleep(1.0)
        img = ig.grab_checked(w)
        s = dialog_score(img, opt)
        if s < opt["dialog_score"]:
            return n
        log(f"     第 {n} 次点「{'是' if confirm == 'yes' else '否'}」没生效（score={s:.3f}），再点")
    return None


def do_option(w, inp, cfg: dict, key: str, confirm: str) -> dict:
    """打一个挑战：开面板 → 悬停后点格子 → 等弹窗 → 点确认。

    2026-11-16 15:03 用 monitor_user.py 录下用户手动开海岛的全过程，证明三件事：
      1) 点 NPC 只把底部面板切到「秘境挑战」页，弹窗**不会自己出来**；
      2) 海岛/不死 = 底部面板的第一/第二格，鼠标**悬停约 1 秒**才浮出
         tooltip「开启海岛秘境 (Q) 点击开启」，此时按下才有效；
      3) 用户全程 2 次点击就进去了：NPC (1254,167) → 格子 → 「是」(773,457)。
    所以这里必须 click_hover（和结算界面「继续观战」同一个坑），不能直接 click。
    旧版点完 NPC 只等 panel_settle 就直点，点击被吞 → 弹窗永远不出来。
    """
    opt = cfg["options"][key]
    res = {"key": key, "label": opt["label"], "dialog": None, "clicks": None, "ok": False}

    for attempt in (1, 2):
        _, _, panel_ok = open_panel(w, inp, cfg)
        if not panel_ok:
            res["error"] = "面板没切到秘境页 → 没成功对话，别点格子"
            return res
        # 点 NPC 万一直接弹窗（不同地图/版本可能不一样），先查一次，别白点
        img0 = ig.grab_checked(w)
        if dialog_score(img0, opt) >= opt["dialog_score"]:
            log(f"   ℹ 点 NPC 后弹窗已直接出现，跳过点格子")
            res["dialog"] = dialog_score(img0, opt)
            break
        # 格子位置用图色现找（多尺度），找不到才用配置里的 xy
        okp, info = panel_secret_open(img0, cfg, extra_keys=(key,))
        # 刚打完 12 格存档挑战时面板要重绘，实测目标格图标会短暂只有 0.69
        # （2026-11-16 19:26 第一轮就是这么失败一次，下一轮同样位置却 1.00）。
        # 面板已经开了但目标格分数不够，就再等两拍重抓，别急着退回写死坐标。
        for _try in (1, 2):
            if not okp or (info[key][0] >= 0.8 and info[key][1]):
                break
            time.sleep(0.7)
            img0 = ig.grab_checked(w)
            okp, info = panel_secret_open(img0, cfg, extra_keys=(key,))
            log(f"   ↻ 格子图标还没画好（{key}={info[key][0]:.3f}）→ 等 0.7s 重抓（第 {_try} 次）")
        tgt = _scale_xy(w, opt["xy"])
        src = "配置xy(缩放)"
        det = info[key][1]
        if okp and det and info[key][0] >= 0.8:
            tgt = det
            src = "图色"
            log(f"   📍 图色定位格子「{opt['label']}」@{tgt}（{info[key][0]:.3f}）")
        else:
            # 这里最容易出问题：目标格图标分数低（例如 0.69）说明它可能**是灰的/不可选**，
            # 不是位置错。把两格分数都打出来，配一张标注图，事后能直接看出来。
            frag = " ".join(f"{k}={v[0]:.3f}@{v[1]}" for k, v in info.items()
                            if k in cfg.get("options", {}))
            log(f"   ⚠ 目标格没认出来（{frag}）→ 回退到{src} {tgt}")
        log(f"   悬停 {cfg.get('icon_hover', 1.2):.1f}s 后点「{opt['label']}」格子 "
            f"{tgt}（第 {attempt} 次）")
        # 用户 2026-11-18 20:10：没出弹窗就在原地重复点（默认 2 次），
        # 不用每次都重开面板 —— 面板被刷掉才走外层第 2 轮。
        tries = max(1, int(cfg.get("icon_retries", 2)))
        ok, s = False, 0.0
        for _k in range(1, tries + 1):
            if _k > 1:
                time.sleep(float(cfg.get("icon_retry_gap", 0.8)))
                log(f"   ↻ 弹窗没出来 → 原地重复点第 {_k}/{tries} 次 {tgt}")
            _click(inp, cfg, *tgt, hover=cfg.get("icon_hover", 1.2))
            ok, s = wait_dialog(w, cfg, key, cfg["wait_dialog"])
            log(f"   弹窗「{opt['dialog_text']}」{'✅ 出现' if ok else '❌ 未出现'}"
                f"（score={s:.3f}, scale={DLG_LAST_SCALE:.2f}）")
            if ok:
                break
        if ok:
            res["dialog"] = s
            break
        if attempt == 2:
            res["error"] = f"弹窗没出现（最高 {s:.3f}）"
            log("   💡 定位/坐标都对、悬停也给了，还是不出弹窗，才轮到这些原因：")
            log("      · 本局已经开过副本（游戏提示「每周只能开启一个副本」）")
            log("      · 角色不在可交互状态 / 面板还没切到「秘境挑战」页")
            # 取证：把两格图标分数 + 实际点的位置画在截图上，事后一眼能看出
            # 是「位置点错」还是「目标格是灰的（不可选）」。
            try:
                dump = img0.copy()
                for k2, v2 in info.items():
                    if k2 not in cfg.get("options", {}) or not v2[1]:
                        continue
                    cv2.circle(dump, tuple(int(x) for x in v2[1]), 34,
                               (0, 0, 255) if v2[0] < 0.8 else (0, 255, 0), 2)
                    cv2.putText(dump, f"{k2} {v2[0]:.2f}", (v2[1][0] - 40, v2[1][1] - 40),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.circle(dump, (int(tgt[0]), int(tgt[1])), 12, (255, 0, 255), 2)
                p = f"logs/e_fail_{key}_{time.strftime('%H%M%S')}.png"
                cv2.imencode(".png", dump)[1].tofile(p)
                log(f"   📸 失败取证图：{p}")
            except Exception as e2:
                log(f"   ⚠ 取证图保存失败：{e2!r}")
            return res

    n = click_confirm(w, inp, cfg, key, confirm)
    res["clicks"] = n
    if n is None:
        res["error"] = f"点「{'是' if confirm == 'yes' else '否'}」{cfg['retry']} 次弹窗仍没关"
        return res
    res["ok"] = True
    log(f"   ✅ 第 {n} 次点击后弹窗关闭（{confirm == 'yes' and '已开启' or '已取消'}）")
    return res


def _wait_screen_changed(w, timeout: float = 6.0):
    """点「是」后等画面真正切过去（传送约 1.2s，黑屏/载入也算变化）。返回耗时或 None"""
    base = ig.grab(w)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.2)
        img = ig.grab(w)
        if base is None or img is None or img.shape != base.shape:
            base = img
            continue
        if float(np.abs(img.astype(np.int16) - base.astype(np.int16)).mean()) > 6.0:
            return round(time.time() - t0, 2)
        base = img
    return None


def run_after_enter(w, inp, cfg: dict, key: str) -> None:
    """进图后的开场操作（配置 after_enter.<key>.steps，2026-11-27 用户录制）

    步骤类型：
      {"wait": 秒}                              等待
      {"click": [x, y]}                         左键单击
      {"right_click": [x, y]}                   右键单击
      {"key": "right", "times": 2, "gap": 0.43} 按键 N 次
    """
    spec = ((cfg.get("after_enter") or {}).get(key) or {})
    steps = spec.get("steps") or []
    if not spec.get("enabled", True) or not steps:
        return
    took = _wait_screen_changed(w)
    if took is None:
        log("   ⏳ 进图后没检测到画面切换（可能还在载入）→ 按定时照常继续")
    else:
        log(f"   🎬 画面已切到副本（{took}s）→ 开始进图后开场操作")
    hold = float(cfg.get("click_hold", 0.09))
    for i, st in enumerate(steps, 1):
        if "wait" in st:
            time.sleep(float(st["wait"]))
            continue
        if "right_click" in st:
            x, y = st["right_click"]
            inp.click(int(x), int(y), "right", hold=hold, clicks=1)
            log(f"   {i}. 右键 ({int(x)},{int(y)})")
            continue
        if "click" in st:
            x, y = st["click"]
            inp.click(int(x), int(y), "left", hold=hold, clicks=1)
            log(f"   {i}. 左键 ({int(x)},{int(y)})")
            continue
        if "key" in st:
            k = str(st["key"])
            n = max(1, int(st.get("times", 1)))
            inp.key(k, presses=n, gap=float(st.get("gap", 0.06)))
            log(f"   {i}. 按 {k} ×{n}")
            continue
    log("   ✅ 进图后开场操作完成")


# ---------------------------------------------------------------- 主流程
def locate_gate_rings(img, text_center, radius=150):
    """在「进入诅咒神庙」文字附近找蓝色光圈（连通块），返回 [(cx, cy, area)]（按 y 排序）。"""
    b, g, r = cv2.split(img)
    mask = ((b > 185) & (g > 95) & (r < 135)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if area < 600 or not (0.5 <= ww / max(hh, 1) <= 2.5):
            continue
        cx, cy = float(cent[i][0]), float(cent[i][1])
        if abs(cx - text_center[0]) <= radius and abs(cy - text_center[1]) <= radius * 1.6:
            out.append((int(cx), int(cy), int(area)))
    out.sort(key=lambda t: t[1])
    return out


def current_difficulty():
    """读大厅记录的当前难度（logs/lobby_difficulty.json，或 %TEMP%/KKAuto 备份）→ int 或 None。

    2026-11-20 19:52 用户要求：**N10 以上才走「进入诅咒神庙」新流程**；
    N1~N9 照旧（点不死挑战 → 等胜利/失败），因为低难度秘境没有那个光圈传送门。
    """
    import json
    paths = []
    base = os.path.dirname(os.path.abspath(__file__))
    paths.append(os.path.join(base, "logs", "lobby_difficulty.json"))
    t = os.environ.get("TEMP")
    if t:
        paths.append(os.path.join(t, "KKAuto", "lobby_difficulty.json"))
    best = 0
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                j = json.load(f)
            # 2026-11-23 13:29 修：英雄模式第 1 行 = N11，不能用行号（1）判断，要看 text_guess
            row = j.get("picked_row") or j.get("picked_index") or 0
            num = 0
            try:
                import re as _re
                _m = _re.match(r"[Nn]?\s*(\d+)", str(j.get("text_guess") or ""))
                if _m:
                    num = int(_m.group(1))
            except Exception:
                num = 0
            best = max(best, num or int(row))
        except Exception:
            continue
    return best or None


def walk_until_gate(w, inp, timeout: float = 90.0, text_th: float = 0.70,
                    max_try: int = 6, wait_switch: float = 5.0,
                    approach_presses: int = 5, gate_delay: float = 5.0,
                    frames: int = 2, rounds: int = 2) -> bool:
    """进不死秘境后：**等 5s → 按 ↑ 走 → 认出「诅咒神庙」文字 → 右键点文字位置 → 等 5s 看是否切换**。

    2026-11-27 用户改口径（截图定案）：
      ① 点「是」进不死秘境后先等 5s（画面加载完）再开始按 ↑
      ② 判据只看「诅咒神庙」文字模板（不再要求「文字+光圈 1+1」，光圈不参与定位）
      ③ 右键**直接点在文字位置上**，不点光圈
      ④ 点了没切换 → 按 ↑ 再靠近一点、重新认文字再点（最多 max_try 次）
    """
    base = os.path.dirname(os.path.abspath(__file__))
    try:
        tpl = cv2.imdecode(np.fromfile(os.path.join(base, "templates", "temple_text.png"),
                                       dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        tpl = None
    if tpl is None:
        log("   ⚠ 缺模板 templates/temple_text.png → 跳过「走向诅咒神庙」")
        return False

    def find_text(img):
        res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _mnl, mxl = cv2.minMaxLoc(res)
        cx = mxl[0] + tpl.shape[1] // 2
        cy = mxl[1] + tpl.shape[0] // 2
        return float(mx), (cx, cy)

    # ---------------- 阶段 1：按 ↑ 走，直到认出「诅咒神庙」文字 ----------------
    if gate_delay > 0:
        log(f"   ⏳ 进秘境后等 {gate_delay:g}s（画面加载完）再开始走…")
        time.sleep(gate_delay)
    t_all = time.time()
    best, best_xy = 0.0, (0, 0)
    presses = 0
    hit_xy, hit_sc = None, 0.0
    for rnd in range(1, max(1, rounds) + 1):
        log(f"   🚶 第 {rnd}/{rounds} 轮：按 ↑ 前进，等「诅咒神庙」文字出现"
            f"（阈值 {text_th:.2f}，连续 {max(1, frames)} 帧）…")
        t0 = time.time()
        hit = 0
        while time.time() - t0 < timeout:
            try:
                img = ig.grab(w)
            except Exception:
                time.sleep(1.0)
                continue
            if img is None:
                time.sleep(0.2)
                continue
            sc, tc = find_text(img)
            if sc > best:
                best, best_xy = sc, tc
            if sc >= text_th:
                hit += 1
                if hit >= max(1, frames):
                    hit_xy, hit_sc = tc, sc
                    break
            else:
                hit = 0
            try:
                inp.key("up", hold=0.12)
            except Exception:
                pass
            presses += 1
            time.sleep(0.10)
        if hit_xy:
            break
        log(f"   ↳ 第 {rnd} 轮 {timeout:.0f}s 没认到（最高 {best:.3f} @{best_xy}，"
            f"累计按了 {presses} 次 ↑）")
    if not hit_xy:
        log(f"   ⚠ {rounds} 轮共 {time.time() - t_all:.0f}s 没认出「诅咒神庙」文字"
            f"（最高 {best:.3f} @{best_xy}，按了 {presses} 次 ↑）→ 放弃神庙，直接等结算")
        return False
    log(f"   ✅ 认出「诅咒神庙」文字 {hit_sc:.3f} @({hit_xy[0]},{hit_xy[1]}) → 右键点文字位置")

    # ---------------- 阶段 2：右键点文字位置 → 等 5s → 没切换就靠近再点 ----------------
    for attempt in range(1, max_try + 1):
        try:
            img = ig.grab(w)
        except Exception:
            img = None
        sc2, tc2 = find_text(img) if img is not None else (0.0, (0, 0))
        if sc2 < text_th:
            # 角色被推挤/走过头，文字暂时认不到 → 再靠近一点重新找
            log(f"   ↳ 文字暂时认不到（{sc2:.3f}）→ 按 ↑ 靠近 {approach_presses} 下再找")
            try:
                for _ in range(approach_presses):
                    inp.key("up", hold=0.12)
                    time.sleep(0.15)
            except Exception:
                pass
            continue
        log(f"   🖱 右键点文字位置 第 {attempt}/{max_try} 次 @({tc2[0]},{tc2[1]})")
        try:
            inp.click(tc2[0], tc2[1], "right", 0.05, 1)
        except Exception as e:
            log(f"   ⚠ 点击失败：{e!r}")
        time.sleep(wait_switch)
        # 判据：文字消失 = 已切换画面
        try:
            img3 = ig.grab(w)
        except Exception:
            img3 = None
        if img3 is not None:
            sc3, _ = find_text(img3)
            if sc3 < 0.60:
                log(f"   ✅ 画面已切换（文字消失 {sc3:.3f}）→ 进入神庙")
                return True
            log(f"   ↳ 还在原地（文字 {sc3:.3f}）→ 按 ↑ 靠近再点")
            try:
                for _ in range(approach_presses):
                    inp.key("up", hold=0.12)
                    time.sleep(0.15)
            except Exception:
                pass
    log(f"   ⚠ 点了 {max_try} 次仍未切换画面")
    return False


def walk_and_hit_altar(w, inp, timeout: float = 60.0, th: float = 0.80,
                       repeat: int = 4, gap: float = 1.0) -> bool:
    """进神庙后：按住 ↑ 走 → 识别「诅咒神庙」建筑 → 右键点 → **重复几次**（打建筑）。

    2026-11-20 18:01 用户教的：进入新画面后再按上键，识别到「诅咒神庙」右键一下，重复几次。
    模板 templates/temple_altar.png（230x175）：自匹配 1.000 / 普通地图 0.317。
    """
    base = os.path.dirname(os.path.abspath(__file__))
    tpl_path = os.path.join(base, "templates", "temple_altar.png")
    try:
        tpl = cv2.imdecode(np.fromfile(tpl_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        tpl = None
    if tpl is None:
        log("   ⚠ 缺模板 templates/temple_altar.png → 跳过「打神庙建筑」")
        return False

    # 2026-11-23 15:08 用户要求：进神庙后先等 3 秒（画面加载完）再按 ↑ 走
    log("   ⏳ 等 3 秒（等神庙画面加载完）…")
    time.sleep(3.0)
    t0 = time.time()
    best = 0.0
    log("   🚶 按 ↑ 走向「诅咒神庙」建筑（用户说约 8 次就到，这里边走边认）…")
    loc = None
    presses = 0
    while time.time() - t0 < timeout:
        try:
            img = ig.grab(w)
        except Exception:
            time.sleep(1.0)
            continue
        if img is not None:
            res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
            _mn, mx, _mnl, mxl = cv2.minMaxLoc(res)
            if float(mx) > best:
                best = float(mx)
            if mx >= th:
                loc = mxl
                log(f"   ✅ 识别到「诅咒神庙」({mx:.3f}，按了 {presses} 次 ↑)")
                break
        # 没认到 → 按 1 下 ↑ 再前进一点（按一下走一小步，不会冲过头）
        try:
            inp.key("up", hold=0.12)
        except Exception:
            pass
        presses += 1
        # 2026-11-23 15:07 用户要求：每按一次 ↑ 间隔 0.5 秒（原来 0.10 秒）
        time.sleep(0.5)

    if loc is None:
        log(f"   ⚠ {timeout:.0f}s 没找到「诅咒神庙」（最高 {best:.3f}）")
        return False

    cx = loc[0] + tpl.shape[1] // 2
    cy = loc[1] + tpl.shape[0] // 2
    for i in range(1, repeat + 1):
        log(f"   🖱 右键点「诅咒神庙」第 {i}/{repeat} 次 @({cx},{cy})")
        try:
            inp.click(cx, cy, "right", 0.05, 1)
        except Exception as e:
            log(f"   ⚠ 点击失败：{e!r}")
        time.sleep(gap)
    log("   ✅ 神庙建筑点完")
    return True


def run(keys: list[str], confirm: str, dry: bool = False) -> int:
    cfg = load_cfg()
    w = wv.bind()

    if dry:
        log("演练模式：只打印计划，不点任何东西")
        for k in keys:
            o = cfg["options"][k]
            log(f"   [{k}] {o['label']}：点图标 {tuple(o['xy'])} → 等「{o['dialog_text']}」"
                f" → 点「{'是' if confirm == 'yes' else '否'}」")
        img = ig.grab(w)
        cur, s = which_dialog(img, cfg)
        log(f"   当前画面弹窗：{cfg['options'][cur]['label'] if cur else '无'}（最高 score={s:.3f}）")
        return 0

    inp = ga.Input(w, True, 0, False, ref_size=ig.REF)
    results = []
    for i, k in enumerate(keys, 1):
        log(f"—— [{i}/{len(keys)}] {cfg['options'][k]['label']} ——")
        r = do_option(w, inp, cfg, k, confirm)
        results.append(r)
        if not r["ok"]:
            log(f"   ❌ {r.get('error')}")
        elif confirm == "yes" and i < len(keys):
            log("   ⚠ 已开启一个秘境副本；游戏提示「每周只能开启一个副本」，后续项可能失败")

    ok = sum(1 for r in results if r["ok"])
    log(f"合计：{ok}/{len(results)} 个挑战" + ("已开启" if confirm == "yes" else "已取消"))
    # 三角洲进图后的开场操作（2026-11-27 用户要求）：
    # 型号在 configs/secret_challenge.json 的 after_enter.delta，改配置不用动代码。
    if confirm == "yes" and any(r.get("key") == "delta" and r.get("ok") for r in results):
        run_after_enter(w, inp, cfg, "delta")
    # 2026-11-20 19:52 用户定：**N10 以上**才走「进入诅咒神庙」新流程；
    # N1~N9 照旧（点不死挑战 → 等胜利/失败，低难度秘境没有光圈传送门）
    # 2026-11-26 16:06 用户：神庙流程**只限不死挑战**；勾海岛就直接等结算（海岛按 ↑ 是多余操作）
    if confirm == "yes" and ok and "undead" in keys:
        diff = current_difficulty()
        if diff is not None and diff >= 10:
            log(f"   ℹ 难度 N{diff}（≥N10，不死挑战）→ 走「进入诅咒神庙」流程")
            _tw = cfg.get("undead_temple") or {}
            if walk_until_gate(w, inp,
                               timeout=float(_tw.get("timeout", 90)),
                               text_th=float(_tw.get("text_th", 0.70)),
                               frames=int(_tw.get("frames", 2)),
                               rounds=int(_tw.get("rounds", 2)),
                               gate_delay=float(_tw.get("gate_delay", 5.0)),
                               max_try=int(_tw.get("max_try", 6)),
                               approach_presses=int(_tw.get("approach_presses", 5)),
                               wait_switch=float(_tw.get("wait_switch", 5.0))):
                walk_and_hit_altar(w, inp)
        else:
            log(f"   ℹ 难度 N{diff if diff else '?'}（<N10）→ 照旧：不走去神庙，直接等结算")
    elif confirm == "yes" and ok:
        log(f"   ℹ 本局是{'/'.join(cfg['options'][k]['label'] for k in keys)}"
            " → 不走去神庙，直接等结算")
    return 0 if ok == len(results) else 1


def wait_resize_then_verify(cfg: dict, timeout: float = 180.0) -> int:
    """盯住客户区尺寸，一旦变化（用户伸缩窗口）就立刻跑一次 verify_ui。

    注意要忽略 0x0：窗口最小化时 client_rect 会返回 0，那不是伸缩，验了也白验。
    """
    w = wv.bind()
    _, _, cw0, ch0 = w.client_rect()
    log(f"等窗口尺寸变化…当前 {cw0}x{ch0}（最长 {timeout:.0f}s）")
    t0 = time.time()
    last = (cw0, ch0)
    while time.time() - t0 < timeout:
        time.sleep(0.5)
        try:
            _, _, cw, ch = w.client_rect()
        except Exception:
            continue
        if cw <= 0 or ch <= 0:
            continue                      # 最小化/隐藏，不算尺寸变化
        if (cw, ch) != last:
            log(f"检测到尺寸变化 {last[0]}x{last[1]} → {cw}x{ch}")
            time.sleep(1.2)               # 等它缩放手稳
            try:
                _, _, cw2, ch2 = w.client_rect()
            except Exception:
                cw2, ch2 = cw, ch
            if cw2 <= 0 or ch2 <= 0 or (cw2, ch2) != (cw, ch):
                continue
            last = (cw2, ch2)
            log(f"尺寸稳定在 {cw2}x{ch2}，开始验证")
            return verify_ui(cfg)
    log("超时：没等到尺寸变化")
    return 4


def probe(cfg: dict) -> int:
    """取证：拉远 + F1 → 定位 → 存图 + 列候选点，**一下都不点**。

    2026-11-18 22:00 那次失败，日志包里只有坐标、没有画面，只能靠猜。
    以后秘境点不开，直接跑 `python secret_challenge.py --probe`，
    把 logs/secret_frame_probe_*.png 发出来就能一眼看出 NPC 在哪。
    """
    w = wv.bind()
    inp = ga.Input(w, True, 0, False, ref_size=ig.REF)
    prepare_view(w, inp, cfg, verbose=True)
    img = ig.grab(w)
    ok, info = panel_secret_open(img, cfg)
    log("面板两格图标：海岛 %.3f / 不死 %.3f（阈值 %.2f）→ %s"
        % (info["island"][0], info["undead"][0], info["threshold"],
           "已经开着" if ok else "没开"))
    loc = locate_npc(img, cfg, verbose=True)
    log("定位结果：%s" % (loc,))
    q = _click_queue(loc, cfg)
    for i, (x, y, src) in enumerate(q, 1):
        log("   候选 %2d: (%d,%d) [%s]" % (i, x, y, src))
    try:
        dump_frame(img, "probe", q)
    except Exception as e:
        log("   ⚠ 存图失败：%s" % e)
    return 0


def verify_ui(cfg: dict) -> int:
    """只做一件事：F1 → 图色定位「秘境挑战」文字 → 点下方光圈 → 判定面板两格图标。

    这就是「成功找到 NPC 并成功对话」的完整验证，不点任何挑战、不点「是」。
    返回 0 = 成功对话；2 = 定位不到 NPC；3 = 点了但面板没切过去。
    """
    w = wv.bind()
    inp = ga.Input(w, True, 0, False, ref_size=ig.REF)
    if cfg.get("press_f1", True):
        log("按 F1 回到人物视角")
        inp.key("f1")
        time.sleep(0.45)
    img = ig.grab(w)
    loc = locate_npc(img, cfg, verbose=True)
    if not loc:
        log("❌ 定位不到「秘境挑战」NPC（文字/模型通道都没过阈值）")
        cv2.imencode(".png", img)[1].tofile("logs/verify_fail.png")
        return 2
    x, y, score, _ = loc
    log(f"点击光圈中心 ({x},{y})，文字 score={score:.3f}")
    _click(inp, cfg, x, y, hover=cfg.get("npc_hover", 0.5))
    time.sleep(cfg["panel_settle"])
    img = ig.grab(w)
    cv2.imencode(".png", img)[1].tofile("logs/verify_after.png")
    ok, info = panel_secret_open(img, cfg)
    log(f"面板判定：海岛格 {info['island'][0]:.3f}@{info['island'][1]}  "
        f"不死格 {info['undead'][0]:.3f}@{info['undead'][1]}  阈值 {info['threshold']}")
    if ok:
        log("✅ 成功找到 NPC 并成功对话（面板已切到秘境页，两格图标都在）")
        return 0
    log("❌ 点了，但面板没切到秘境页（两格图标未同时出现）")
    return 3


def main():
    global CFG_PATH
    ap = argparse.ArgumentParser(description="秘境挑战：海岛 / 不死 / 三角洲（按勾选执行）")
    ap.add_argument("--config", default=CFG_PATH)
    ap.add_argument("--island", action="store_true", help="勾上海岛挑战")
    ap.add_argument("--no-island", action="store_true", help="取消海岛")
    ap.add_argument("--undead", action="store_true", help="勾上不死挑战")
    ap.add_argument("--no-undead", action="store_true", help="取消不死")
    ap.add_argument("--delta", action="store_true", help="勾上三角洲挑战（潮汐监狱，2026-11-27 新增）")
    ap.add_argument("--no-delta", action="store_true", help="取消三角洲")
    ap.add_argument("--save", action="store_true", help="把本次勾选写回 json（模拟软件打钩）")
    ap.add_argument("--go", action="store_true", help="真点「是」（默认就是，保留兼容）")
    ap.add_argument("--no", action="store_true", help="演练：弹窗点「否」（测试定位用）")
    ap.add_argument("--dry", action="store_true", help="只打印计划，不做任何点击")
    ap.add_argument("--check", action="store_true", help="只打印当前勾选")
    ap.add_argument("--verify", action="store_true", help="只验证：F1→定位文字→点光圈→查两格图标")
    ap.add_argument("--probe", action="store_true",
                    help="只取证：滚轮拉远+F1 → 定位 → 存 logs/secret_frame_probe_*.png 并列出候选点，一下都不点")
    ap.add_argument("--verify-watch", type=float, default=0, metavar="SEC",
                    help="等客户区尺寸变化后再验证（伸缩窗口用），SEC=超时秒数")
    args = ap.parse_args()
    CFG_PATH = args.config
    cfg = load_cfg(args.config)

    # 命令行勾选覆盖配置（等价于软件里点复选框）
    if args.island:
        cfg["options"]["island"]["checked"] = True
    if args.no_island:
        cfg["options"]["island"]["checked"] = False
    if args.undead:
        cfg["options"]["undead"]["checked"] = True
    if args.no_undead:
        cfg["options"]["undead"]["checked"] = False
    if args.delta:
        cfg["options"]["delta"]["checked"] = True
    if args.no_delta:
        cfg["options"]["delta"]["checked"] = False
    if args.save:
        save_cfg(cfg, args.config)
        log(f"已写回 {args.config}")

    marks = [(k, cfg["options"][k]) for k in KEYS]
    log("勾选状态：" + "  ".join(
        f"[{'√' if o['checked'] else ' '}] {o['label']}" for _, o in marks))

    if args.check:
        return 0

    if args.probe:
        return probe(cfg)

    if args.verify:
        return verify_ui(cfg)

    if args.verify_watch:
        return wait_resize_then_verify(cfg, args.verify_watch)

    keys = checked_keys(cfg)
    if not keys:
        log("一个都没勾 —— 什么都不做（软件里就是「都不打」）")
        return 0

    # 默认点是（实际应用）；--no 才走演练
    confirm = "no" if args.no else ("yes" if args.go else cfg.get("confirm_mode", "yes"))
    log(f"确认方式：{'点「是」= 真打（角色会被传送）' if confirm == 'yes' else '点「否」= 演练（不真开）'}")
    try:
        return run(keys, confirm, args.dry)
    except KeyboardInterrupt:
        log("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
