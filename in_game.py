# -*- coding: utf-8 -*-
"""
游戏内四步一键执行（用户 2026-11-16 00:45 归纳）

  ① 技能三选一：按 G → 双击中间卡 → 面板关闭        （循环 3 次）
  ② 装备三选一：按 F → 双击卡   → 面板关闭          （循环 4 次）
  ③ 升级武器  ：右键装备栏 1 号格 → 双击            （循环 3 次，花金币）
  ④ 自动挑战  ：右键「主线挑战」图标 → 移开鼠标校验「第X波(自动)」，没出现就重试

坐标均基于客户区 1690x921：
  技能卡中间  (840, 338)
  装备卡安全点 (800, 490)   ← 卡片数量 1~3 张都落在卡内
  武器格 1 号  (1151, 750)
  主线挑战图标 (293, 632)

用法：
  python in_game.py                  # 四步全跑
  python in_game.py --steps 1,2      # 只跑技能和装备
  python in_game.py --skip 3         # 跳过升级武器（省金币）
  python in_game.py --skill 3 --equip 4 --upgrade 3
"""
from __future__ import annotations

import argparse
import time

import cv2
import numpy as np
import win32api
import win32con
import win32gui
from PIL import ImageGrab

import game_auto as ga
import wave_auto as wv

# ---- 坐标 ----
P_SKILL = (840, 338)
P_EQUIP = (800, 490)
P_WEAPON = (1151, 750)
REG_SKILL = (730, 108, 950, 400)      # (x0,y0,x1,y1) 技能卡面板区
REG_GRID = (650, 230, 1050, 580)      # 「自选技能」12 格面板区（2026-11-18 新增）
REG_EQUIP = (760, 120, 1000, 380)     # 装备卡面板区
DARK_OPEN = 0.40                      # 深色占比 > 0.40 → 面板开着

LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


# 所有坐标 / 区域 / 模板都是按客户区 1690x921 调好的。换到分辨率更低的电脑
# （游戏窗口只有 1000x580 这类）时，把截图统一缩放回 1690x921 再识别，点击时按
# 「实际客户区 / 基准」放大回去 —— 这样 12 个写死的坐标常量和全部模板都不用改。
# 用户 2026-11-17 18:32 在另一台电脑实测踩的就是这个（窗口约 0.63 倍）。
REF = (1690, 921)
_NORM_LOGGED = [False]

# 独立跑本脚本（不经 GUI）时也要 per-monitor DPI aware：否则 125%/150% 缩放的电脑上
# ImageGrab 拿到的是系统虚拟化放大过的画面，模板分数会掉一截。已设过则空转无害。
_DPI_DONE = [False]


def ensure_dpi():
    if _DPI_DONE[0]:
        return
    _DPI_DONE[0] = True
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def norm(img):
    """把截图归一化到 REF 尺寸；只有尺寸不同才缩放（宽高比差异 2% 内可忽略）"""
    h, w = img.shape[:2]
    if (w, h) == REF:
        return img
    interp = cv2.INTER_AREA if w > REF[0] else cv2.INTER_CUBIC
    out = cv2.resize(img, REF, interpolation=interp)
    if not _NORM_LOGGED[0]:
        _NORM_LOGGED[0] = True
        log(f"   窗口客户区 {w}x{h} ≠ 基准 {REF[0]}x{REF[1]} → 截图统一缩放到基准再识别，"
            f"点击按 {w / REF[0]:.2f}x{h / REF[1]:.2f} 倍放大回去")
        # 宽高比差太多（比如 4:3 屏 / 被拉到很窄）时，等比「拉伸」会让模板变形、匹配分数掉，
        # 而且游戏渲染区可能是居中有黑边的。这种情况给一句明确建议，比让他反复重试强。
        try:
            ratio = (w / REF[0]) / (h / REF[1])
            if abs(ratio - 1) > 0.08:
                log(f"   ⚠ 窗口宽高比与基准差 {(ratio - 1) * 100:+.0f}%（{w}x{h} vs 1690x921）："
                    f"识别率可能偏低。建议把游戏分辨率设成 16:9（如 1600x900 / 1280x720）")
        except Exception:
            pass
    return out


def grab(w):
    ensure_dpi()
    # War3 会莫名其妙最小化（切分辨率 / 全屏切换 / 被系统弹窗挤下去）。这时
    # GetClientRect 给 0x0，截图直接 empty(0,0,3)，4 次重试全废 → 上层判「窗口关了」
    # 整段中止（2026-11-18 00:46 这一局 C 阶段就是这么挂的）。先恢复再截。
    try:
        if win32gui.IsIconic(w.hwnd):
            win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
            time.sleep(0.7)
            log("   ↳ War3 窗口被最小化 → 已恢复")
    except Exception:
        pass
    # 平台窗口（房间/主窗/弹窗）被别的东西顶到前台时，截到的就是平台画面而不是游戏，
    # dark 判据会直接误判（实测：被平台盖住时 dark=0.99，被当成「面板还开着」）。
    try:
        if not w.fg_same_process():
            w.focus()
            time.sleep(0.35)
    except Exception:
        pass
    cx, cy, cw, ch = w.client_rect()
    last = None
    # 2026-11-21 12:52 用户要求：截图失败 → 先恢复窗口（SW_RESTORE/SW_SHOW）→ 等 3 秒再抓
    # → 连续 3 次都失败才中断（不再 1 次就中止，窗口短暂最小化/隐藏能自己恢复继续跑）
    for attempt in range(3):
        try:
            arr = np.array(ImageGrab.grab(bbox=(cx, cy, cx + cw, cy + ch)))
            if arr.size and arr.shape[0] > 10:
                # 纯色/全黑说明窗口被最小化、被别的窗口完全盖住、或显卡独占输出 —— 这时候
                # 继续「识别 → 点空白」比报错更糟，所以当无效重试。阈值放得很松（std<1.0），
                # 加载黑屏带 logo/进度条的画面不会被误杀。
                try:
                    sd = float(arr.reshape(-1, arr.shape[2]).std(axis=0).max())
                except Exception:
                    sd = 99.0
                if sd >= 1.0:
                    return norm(cv2.cvtColor(arr, cv2.COLOR_RGB2BGR))
                last = f"纯色画面 std={sd:.2f}（窗口可能被最小化/遮挡）"
            else:
                last = f"empty {arr.shape}"
        except Exception as e:      # 窗口被关掉/最小化时 ImageGrab 会抛错
            last = repr(e)
        # 失败 → 恢复窗口（最小化用 RESTORE，隐藏用 SHOW）→ 等 3 秒再抓
        try:
            win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
            win32gui.ShowWindow(w.hwnd, win32con.SW_SHOW)
            _tail = "，最后一次仍失败 → 直接中断" if attempt >= 2 else "，等 3 秒再抓"
            log(f"   ↳ 截图失败（{last}）→ 恢复窗口（第 {attempt + 1}/3 次）{_tail}")
        except Exception:
            pass
        try:
            cx, cy, cw, ch = w.client_rect()      # 恢复后客户区尺寸会变，重新取
        except Exception:
            pass
        # 2026-11-21 13:07 用户定：最后一次失败不再等，直接中断（省 3 秒）
        if attempt < 2:
            time.sleep(3.0)
    # 截图彻底失败也可能是「游戏已经结束」态（窗口被平台换成 wyplatform_war3_game）：
    # 兜底找窗口 → 点「返回平台」→ 抛 MARK 让上层重开
    try:
        import end_guard as eg
        if eg.recover():
            raise RuntimeError(eg.MARK)
    except RuntimeError:
        raise
    except Exception:
        pass
    raise RuntimeError(f"截图失败（War3 窗口可能已关闭、最小化，或被全屏独占挡住）：{last}"
                       f" ｜ 若游戏是全屏模式，请改成「窗口模式」再跑")


def grab_checked(w):
    """截图 + 顺手查「作弊风险」提示：命中就直接结束 War3 并中断（这时游戏已结束，继续点全是空点）"""
    img = grab(w)
    try:
        import cheat_guard as cg
        s = cg.score(img)
        if s >= cg.SCORE:
            log(f"⛔ 检测到「官方检测该账号有作弊风险」提示（score={s:.3f}）→ 结束 War3 进程")
            cg.kill_war3(verbose=False)
            raise RuntimeError("检测到作弊风险提示，已结束 War3（风控中断）")
    except RuntimeError:
        raise
    except Exception:
        pass
    # 顺手查「游戏已经结束…+ 返回平台」：点「返回平台」回平台，抛异常让上层重开
    # （用户 2026-11-16 23:03：出现这个选「返回平台」，重开游戏继续）
    try:
        import end_guard as eg
        if eg.guard(img, w):
            raise RuntimeError(eg.MARK)
    except RuntimeError:
        raise
    except Exception:
        pass
    return img


def dark_ratio(img, reg):
    x0, y0, x1, y1 = reg
    g = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    return float((g < 60).mean())


def park(w, pos=(380, 700)):
    cx, cy, _, _ = w.client_rect()
    win32api.SetCursorPos((cx + pos[0], cy + pos[1]))


def panel_open(img, reg):
    return dark_ratio(img, reg) > DARK_OPEN


def grid_cells(img, reg=REG_GRID):
    """识别「自选技能」12 格面板里**可点**的格子中心，按 行→列 排序。

    2026-11-18 02:37 用户反馈：按 G 弹出的不是三张卡，而是 12 宫格「自选技能」。
    旧逻辑双击固定点 (840,338) 正好落在格子间隙（实测 x 中心 704/797/890/984，
    y 中心 316/419/515，间距约 93x100）→ 双击空气，面板不关，3 次重试全废。
    实测：(840,338) 距最近格子中心 (-43,-22)/(50,-17)，确实在缝里。

    这里用高饱和彩色连通块找格子：带红 X 的「不可选」格子是灰暗的，饱和度和亮度都低，
    天然被过滤掉（实测本局 12 格里只检出 10 格，正好排除了两个红 X）。
    返回空列表 = 当前不是 12 格面板（走原来的三选一两张卡逻辑）。
    """
    x0, y0, x1, y1 = reg
    roi = img[y0:y1, x0:x1]
    if roi.size == 0:
        return []
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m = ((hsv[:, :, 1] > 110) & (hsv[:, :, 2] > 90)).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, _, st, ct = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        w, h, a = st[i][2], st[i][3], st[i][4]
        if 50 <= w <= 130 and 45 <= h <= 110 and a > 1600:
            out.append((int(ct[i][0]) + x0, int(ct[i][1]) + y0))
    out.sort(key=lambda p: (round(p[1] / 50.0), p[0]))
    return out


def wait_ready(w, timeout=25.0, max_dark=0.35):
    """等游戏画面就绪：加载/黑屏时 dark 也会 >0.4，会被误判成「面板开着」。
    这里先用技能区做探针，等它变亮（dark < 0.35）才开始操作。

    ⚠️ 要连续 2 次探针都通过才算就绪（间隔 ~1s）：大厅/载入画面也可能很亮（dark≈0.07），
    单次通过就开干的话，四步会在大厅上空点（日志里那种「已进入地图 dark=0.07 等待 0s」）。
    """
    t0 = time.time()
    hits = 0
    last_d = 1.0
    while time.time() - t0 < timeout:
        try:
            last_d = dark_ratio(grab_checked(w), REG_SKILL)
        except RuntimeError as e:
            if "作弊" in str(e):
                raise
            hits = 0
            time.sleep(1.0)
            continue
        if last_d < max_dark:
            hits += 1
            if hits >= 2:
                return True, last_d
        else:
            hits = 0
        time.sleep(1.0)
    return False, last_d


# ---------- ①② 通用：按键开面板 → 双击固定点 ----------
def open_and_pick(w, inp, key, reg, point, times, label):
    """循环 times 次：按 key 开面板 → 双击 point → 确认面板关闭。返回成功次数。"""
    ok = 0
    for i in range(1, times + 1):
        w.focus()
        time.sleep(0.35)
        park(w)
        img = grab_checked(w)
        if panel_open(img, reg):          # 上次没关干净，先点空白收起
            log(f"   {label}第{i}轮：面板本来就开着，先收起")
            inp.click(point[0], point[1] + 60, "left", 0.05, 1)
            time.sleep(0.8)

        opened = False
        for attempt in range(4):
            w.focus()
            time.sleep(0.25)
            park(w)
            inp.key(key)
            # 等待递增：面板弹出/掉帧时 1.3 秒可能不够，最后一次给到 3.2 秒。
            # 实测第 3 轮经常前两次按不弹、第三次才出来（不是没技能点，是按键被吃掉），
            # 所以给到 4 次，别把技能点浪费掉。
            time.sleep(1.3 + attempt * 0.7)
            img = grab_checked(w)
            d = dark_ratio(img, reg)
            if panel_open(img, reg):
                opened = True
                break
            log(f"   {label}第{i}轮：按 {key.upper()} 没弹面板(尝试{attempt + 1}/4, dark={d:.2f})")
            time.sleep(0.4)
        if not opened:
            # 留证据：为什么没弹（英雄没升级/没技能点，还是按键没送到）
            try:
                import os as _os
                d = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "logs")
                _os.makedirs(d, exist_ok=True)
                p = _os.path.join(d, f"miss_{label}_{i}_{time.strftime('%H%M%S')}.png")
                # ⚠️ 不能用 cv2.imwrite：含中文的文件名在 Windows 上会静默失败（返回 False、不报错），
                # 之前"现场图 logs\miss_技能_3_xxx.png"就是这么丢的。改用 imencode + tofile。
                okenc, buf = cv2.imencode(".png", img)
                if not okenc:
                    raise RuntimeError("png 编码失败")
                buf.tofile(p)
                log(f"   {label}第{i}轮：⚠️ 面板始终没出来，跳过（现场图 logs/{_os.path.basename(p)}）")
            except Exception as e:
                log(f"   {label}第{i}轮：⚠️ 面板始终没出来，跳过（现场图没存成：{e}）")
            continue

        # 双击选卡：面板没关 = 双击没生效（卡片还没画好/点击被吃），再补点几次。
        # 实测第 1 轮最容易失败（面板刚弹出还在动画），补点一次基本就成。
        picked, d = False, 1.0
        for k in range(3):
            w.focus()
            time.sleep(0.2)
            park(w)
            # 12 格「自选技能」面板（用户 2026-11-18 遇到）：固定点会落在格子缝里，
            # 必须按识别出的格子中心点；没有格子才回落到原固定点（三选一两张卡）。
            tx, ty = point
            try:
                _cells = grid_cells(grab_checked(w))
            except Exception:
                _cells = []
            # 只有真正的 12 格面板才走网格（实测检出 10 格）；三选一/装备面板里
            # 也会误检出 1~2 个彩色块，那时抢在固定点前面反而点不中（实测装备 2/4）。
            if len(_cells) >= 6:
                # 用户 2026-11-18 02:43 定：12 格面板**只点第一格（左上角）**就行，
                # 不要轮换 —— 轮换到第 2/3 格时那几格常常是灰色「不可选」，
                # 双击过去没反应，白等 1.3s 再重试。第一格永远是最先解锁的主动技能。
                tx, ty = _cells[0]
                if k == 0:
                    log(f"   {label}第{i}轮：识别到「自选技能」{len(_cells)} 格面板 → 双击格子 ({tx},{ty})")
            inp.click(tx, ty, "left", 0.05, 2)
            time.sleep(1.3 + k * 0.4)
            img = grab_checked(w)
            d = dark_ratio(img, reg)
            if not panel_open(img, reg):
                picked = True
                break
            log(f"   {label}第{i}轮：双击没关掉面板（第{k + 1}次, dark={d:.2f}）→ 再点一次")
        if picked:
            ok += 1
            log(f"   {label}第{i}轮：✅ 双击 ({tx},{ty}) 后 dark={d:.2f}，面板已关")
        else:
            log(f"   {label}第{i}轮：❌ 双击 3 次面板仍开着 (dark={d:.2f})，先收起")
            park(w)
            inp.click(point[0], point[1] + 60, "left", 0.05, 1)
            time.sleep(0.8)
        time.sleep(0.9)
    return ok


# ---------- ③ 右键双击升级武器 ----------
def upgrade_weapon(w, inp, times):
    ok = 0
    for i in range(1, times + 1):
        w.focus()
        time.sleep(0.3)
        park(w)
        time.sleep(0.2)
        inp.click(P_WEAPON[0], P_WEAPON[1], "right", 0.04, 2)
        time.sleep(1.2)
        park(w)
        ok += 1
        log(f"   升级武器第{i}次：右键双击 ({P_WEAPON[0]},{P_WEAPON[1]})")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="1,2,3,4", help="要跑哪几步，逗号分隔")
    ap.add_argument("--skip", default="", help="跳过哪几步，逗号分隔")
    ap.add_argument("--skill", type=int, default=3)
    ap.add_argument("--equip", type=int, default=4)
    ap.add_argument("--upgrade", type=int, default=3)
    ap.add_argument("--auto-retries", type=int, default=5)
    args = ap.parse_args()

    steps = {int(x) for x in args.steps.split(",") if x.strip()}
    steps -= {int(x) for x in args.skip.split(",") if x.strip()}
    log(f"计划执行步骤：{sorted(steps)}")

    w = wv.bind()
    if w is None:
        log("❌ 没找到 War3 窗口")
        return 2
    inp = ga.Input(w, True, 0, False, ref_size=REF)
    w.focus()
    time.sleep(0.4)
    ready, d = wait_ready(w)
    log(f"画面就绪检查：dark={d:.2f} → {'✅ 可以开始' if ready else '⚠️ 25 秒内画面仍然很暗（可能还在加载），仍继续尝试'}")
    t0 = time.time()
    summary = {}

    if 1 in steps:
        log("① 技能三选一（G + 双击中间卡）")
        summary["技能"] = f"{open_and_pick(w, inp, 'g', REG_SKILL, P_SKILL, args.skill, '技能')}/{args.skill}"

    if 2 in steps:
        log("② 装备三选一（F + 双击卡）")
        summary["装备"] = f"{open_and_pick(w, inp, 'f', REG_EQUIP, P_EQUIP, args.equip, '装备')}/{args.equip}"

    if 3 in steps:
        log("③ 升级武器（右键双击装备 1 号格）")
        summary["升级"] = f"{upgrade_weapon(w, inp, args.upgrade)}/{args.upgrade} 次"

    if 4 in steps:
        log("④ 自动挑战（右键图标 + 校验「自动」）")
        on, n = wv.is_auto_on(_grab_after_park(w))
        if on:
            log(f"   已经是自动状态（红字{n}），不需要右键")
            summary["自动挑战"] = "已开启"
        else:
            done = False
            for i in range(1, args.auto_retries + 1):
                w.focus()
                time.sleep(0.25)
                park(w)
                time.sleep(0.2)
                inp.click(wv.ICON[0], wv.ICON[1], "right", 0.05, 1)
                park(w)
                time.sleep(1.2)
                on, n = wv.is_auto_on(_grab_after_park(w))
                log(f"   右键第{i}次 → 红字像素={n} → {'✅ 出现「自动」' if on else '❌ 没出现'}")
                if on:
                    done = True
                    break
                time.sleep(0.5)
            summary["自动挑战"] = "已开启" if done else "❌ 失败"

    cost = time.time() - t0
    log("")
    log("==== 四步执行完毕 ====")
    for k, v in summary.items():
        log(f"  {k}：{v}")
    log(f"总耗时 {cost:.1f} 秒")
    return 0

def _grab_after_park(w):
    park(w, wv.PARK)
    time.sleep(0.9)
    return grab_checked(w)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as e:
        print(f"\n⚠️ 执行中断：{e}")
        raise SystemExit(3)
