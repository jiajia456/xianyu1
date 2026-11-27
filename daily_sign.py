# -*- coding: utf-8 -*-
"""平台每日签到（KK官方对战平台）—— 一键签到 / 幂等检查

用户 2026-11-17 01:52 教的流程：
  「点击签到，弹窗之后看到『立即签到』就双击点击一下」
  未签到：弹窗底部是蓝色「立即签到」→ 双击 → 出「签到成功/已累计签到 N 天」浮层
  已签到：弹窗底部变灰「今日已签到」，第一天格子显示「已领取」

流程：
  1) 找平台主窗（标题含「对战平台」，取面积最大那个）
  2) 抓图缩到宽 1080，匹配 templates/tpl_btn_sign.png 找「签到」按钮 → 点击
     （匹配不上就退回固定坐标 231,151，1080 基准）
  3) 等独立顶层弹窗（Qt 弹窗，1024x598，标题同主窗）出现
  4) 弹窗里匹配「立即签到」tpl_sign_immediate.png：命中 → 双击
     匹配「今日已签到」tpl_sign_done.png：命中 → 说明今天签过了，直接收尾
  5) 等 1.5s 让奖励浮层消失，再匹配一次确认「已领取」；最后 ESC 关掉弹窗

用法：
  python daily_sign.py              # 签到（已签则跳过）
  python daily_sign.py --check      # 只看状态：已签/未签，不点任何东西
  python daily_sign.py --keep       # 签完不关弹窗
  python daily_sign.py --shot DIR   # 把过程截图存到 DIR（默认 logs/）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import cv2
import numpy as np
import win32api
import win32con
import win32gui

import popup_guard as pg

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_SIGN = os.path.join(HERE, "templates", "tpl_btn_sign.png")
TPL_IMM = os.path.join(HERE, "templates", "tpl_sign_immediate.png")
TPL_DONE = os.path.join(HERE, "templates", "tpl_sign_done.png")
BASE_W = 1080.0
SIGN_XY = (231.0, 151.0)          # 1080 基准的签到按钮兜底坐标
SIGN_SCORE = 0.80
PANEL_SCORE = 0.85


def save_img(path, img):
    """存证截图。任何失败都不能影响主流程（本环境覆盖既有文件会被拒）。"""
    if img is None:
        return None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except Exception:
        pass
    root, ext = os.path.splitext(path)
    for p in (path, "%s_%s%s" % (root, time.strftime("%H%M%S"), ext or ".png")):
        try:
            cv2.imencode(".png", img)[1].tofile(p)      # 中文路径只能用 imencode
            return p
        except Exception:
            continue
    return None


def snap1080(hwnd):
    img = pg.grab_hwnd(hwnd)
    if img is None:
        return None, 1.0
    k = BASE_W / img.shape[1]          # 缩小比（1080 基准 ← 真实）
    small = cv2.resize(img, (int(BASE_W), int(round(img.shape[0] * k))),
                       interpolation=cv2.INTER_AREA)
    return small, k


def find_platform():
    cands = pg.enum_candidates(False)
    if not cands:
        return None
    return cands[0][0]


def click_client(hwnd, cx, cy, hold=0.05, jitter=True):
    """客户区坐标 → 屏幕坐标后点击"""
    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    sx, sy = int(round(ox + cx)), int(round(oy + cy))
    pg.click_screen(hwnd, sx, sy, hold=hold)
    return sx, sy


def double_click_client(hwnd, cx, cy, gap=0.12):
    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    sx, sy = int(round(ox + cx)), int(round(oy + cy))
    pg.click_screen(hwnd, sx, sy, hold=0.05)
    time.sleep(gap)
    pg.click_screen(hwnd, sx, sy, hold=0.05)
    return sx, sy


def _is_room_window(hwnd):
    """这个窗口是不是平台「游戏房间」（它不是签到弹窗）"""
    try:
        import plat_home as ph
        f, _ = ph.face_of(pg.grab_hwnd(hwnd))
        return f == "room"
    except Exception:
        return False


def _reuse_detail(n):
    """父进程刚确认过的详情页 hwnd → 廉价校验后直接复用（不做任何模板匹配）。

    只认「窗口还在 + 可见 + 客户区没被压小」：详情页是 1332x945，
    最小化时 GetClientRect 是 0x0、被压到只剩标题栏时 h=28，都会被挡掉。
    内容对不对不在这里赌 —— 后面「签到」按钮模板匹配不到会自动重新校正一次。
    """
    if not n:
        return None
    try:
        if not (win32gui.IsWindow(n) and win32gui.IsWindowVisible(n)):
            return None
        w, h = win32gui.GetClientRect(n)[2:]
        if w < 1000 or h < 700:
            return None
        return n
    except Exception:
        return None


def clear_ads(verbose=True):
    """先清掉随机广告弹窗（首充礼包 / 216U币红包），返回是否关掉了一个。

    2026-11-19 00:07 实测踩坑：打完一局「退出游戏」后平台弹了「首充礼包 连锁三天」
    **独立小窗**（924x562，标题也是「KK官方对战平台」）。它比主窗小、又不是房间窗，
    于是被 find_panel() 的 fallback 当成「签到弹窗已经开着」；里面既没有「立即签到」
    也没有「今日已签到」→ 直接返回 5，整段【签到领券】白跑（② 领券也一并跳过）。
    用户 2026-11-17 已定规则：这类随机广告直接点右上角 X 关掉。
    """
    try:
        import ad_guard
    except Exception as e:
        if verbose:
            print(f"   (广告守卫不可用：{e!r})")
        return False
    try:
        r = ad_guard.close(verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"   (广告守卫出错：{e!r})")
        return False
    if r == "failed" and verbose:
        print("   ⚠️ 认出广告弹窗但没关掉 → 继续按原流程走")
    return r == "closed"


def _panel_kind(panel):
    """只抓一帧判内容（先确认「已经开着的那个」到底是不是签到弹窗，别急着当成它）"""
    if panel is None:
        return None
    return panel_state(pg.grab_hwnd(panel))[0]


def find_panel(main_hwnd, timeout=6.0):
    """等签到弹窗：同标题、尺寸明显小于主窗的另一个顶层窗"""
    t0 = time.time()
    main_rect = win32gui.GetClientRect(main_hwnd)
    main_area = main_rect[2] * main_rect[3]
    try:
        import win32process
        _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)
    except Exception:
        main_pid = None
    fallback = None
    while time.time() - t0 < timeout:
        for hwnd, title, size in pg.enum_candidates(False):
            if hwnd == main_hwnd:
                continue
            if size[0] * size[1] >= main_area * 0.95:
                continue
            # 签到弹窗固定 1024x598；先认这个，别把 924x562 的首充/活动弹窗当成签到
            if abs(size[0] - 1024) <= 60 and abs(size[1] - 598) <= 40:
                if pg.grab_hwnd(hwnd) is not None:
                    return hwnd, size
            elif fallback is None:
                # 2026-11-18 14:06 实测：平台房间窗（1224x904）也会落进 fallback，
                # 被当成「签到弹窗已开着」→ 签到不找按钮直接放弃。
                # 先按尺寸快速排（房间窗固定 1224x904），再用形态确认一次。
                if abs(size[0] - 1224) <= 14 and abs(size[1] - 904) <= 14:
                    continue
                if _is_room_window(hwnd):
                    continue
                # 2026-11-20 01:20：fallback 必须是平台进程自己的窗口 —— 资源管理器
                # 「KK日志_xxx」（1111x633）曾被当成「签到弹窗已开着」直接放弃签到。
                if main_pid is not None:
                    try:
                        import win32process
                        if win32process.GetWindowThreadProcessId(hwnd)[1] != main_pid:
                            continue
                    except Exception:
                        continue
                fallback = (hwnd, size)
        time.sleep(0.4)
    if fallback and pg.grab_hwnd(fallback[0]) is not None:
        return fallback
    return None, None


def panel_state(img):
    """返回 ('todo'|'done'|None, 按钮中心坐标)"""
    if img is None:
        return None, None
    for tag, tpl in (("todo", TPL_IMM), ("done", TPL_DONE)):
        m = pg.match(img, tpl, PANEL_SCORE)
        if m and m[0] >= PANEL_SCORE:
            return tag, m[1]
    return None, None


def wait_ready(panel, timeout=8.0, settle=1.4):
    """等弹窗稳定：先避开纯黑帧（01:58）和加载过渡帧（02:17 发现横幅+二维码版，
    「立即签到」在 (499,175)，稳定后才切成七天格子版、按钮在 (499,530)）。
    做法：先等 settle 秒让动画跑完，再连续两次抓图结果一致才认。"""
    time.sleep(settle)
    t0 = time.time()
    img = None
    last = None
    while time.time() - t0 < timeout:
        img = pg.grab_hwnd(panel)
        if img is None or float(img.std()) <= 8:
            time.sleep(0.35)
            continue
        tag, c = panel_state(img)
        if tag:
            if last is not None and last[0] == tag and \
                    abs(last[1][0] - c[0]) <= 6 and abs(last[1][1] - c[1]) <= 6:
                return img, tag, c            # 两次一致 → 认为动画结束
            last = (tag, c)
        else:
            last = None
        time.sleep(0.4)
    tag, c = panel_state(img)
    return img, tag, c


def hold_then_close(panel, hold, t0):
    """用户规则（01:56）：识别出状态/签完之后停 5 秒再关窗"""
    left = hold - (time.time() - t0)
    if left > 0:
        print(f"⏳ 停 {left:.1f}s 再关窗（规则：看完状态/奖励留 5 秒）")
        time.sleep(left)
    print(f"🧹 关闭弹窗 → {'成功' if close_panel(panel) else '没关掉'}")


def close_panel(hwnd, tries=3, verbose=True):
    """关签到弹窗：优先点右上角 X（比例坐标 0.9755 / 0.0435），失败再 ESC，最后 WM_CLOSE。

    2026-11-17 02:03 实测：ESC 在「刚打开」和「已签」时都能关，但**签到成功那一瞬间**
    连发 4 次 ESC 都不动；此时真实鼠标点 X 一次就关掉了。所以 X 优先、ESC 兜底。
    """
    def alive():
        return win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)

    for i in range(max(1, tries)):
        if not alive():
            return True
        r = win32gui.GetClientRect(hwnd)
        ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
        x = int(ox + r[2] * 0.9755)
        y = int(oy + r[3] * 0.0435)
        win32api.SetCursorPos((x, y))
        time.sleep(0.2)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.8)
        if not alive():
            return True
        # 兜底：ESC
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.2)
        win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
        time.sleep(0.06)
        win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.7)
        if not alive():
            return True
        if verbose:
            print(f"   ↻ 第 {i + 1} 轮（点X + ESC）没关掉，再试")
    if alive():
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(0.8)
    return not alive()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只看状态")
    ap.add_argument("--keep", action="store_true", help="签完不关弹窗")
    ap.add_argument("--hold", type=float, default=5.0, help="看完状态后等几秒再关窗（默认 5）")
    ap.add_argument("--shot", default=os.path.join(HERE, "logs"))
    ap.add_argument("--detail-hwnd", type=int, default=0,
                    help="父进程刚确认过的「地图详情」窗口句柄；给了就跳过重复的全窗形态扫描")
    a = ap.parse_args()

    pg.enable_dpi_awareness()
    # 登录窗口守卫：掉线了就别点签到（只检测记录，不动手）
    try:
        import login_guard as _lg
        _hit = _lg.check("签到前")
        if _hit:
            print(f"⛔ {_hit.get('reason') or '检测到登录窗口'}：{_hit.get('ts')} → 停止，不做任何操作")
            return 131
    except Exception:
        pass

    # 2026-11-18 13:56 用户指出：打完一局「退出游戏」后屏幕停在**游戏房间**窗口，
    # 里面没有顶部「签到」和 tab「任务」→ 先带回「地图详情」页再往下走
    # 2026-11-19 降 CPU：daily_claim 刚用同一个函数确认过「已在详情页」，并把 hwnd
    # 传了进来 → 这里不用再全窗扫一遍。一次 scan 实测 0.5s（face_of 里 7 个模板匹配），
    # 父进程扫一次、子进程又扫一次，同 1 秒内对同一个窗口白花一倍。
    if a.detail_hwnd:
        print(f"\u267b\ufe0f 父进程已确认详情页 hwnd={a.detail_hwnd} → 跳过重复的形态扫描")
    else:
        try:
            import plat_home as ph
            _r = ph.ensure_detail(verbose=True)
            if not _r.get("ok"):
                print(f"   \u26a0\ufe0f 平台没回到「地图详情」页（{_r.get('reason')}）——后面可能找不到按钮")
        except Exception as _e:
            print(f"   (平台形态校正跳过：{_e!r})")

    # 广告弹窗先清掉：退出游戏后平台经常随机弹「首充礼包」，它是模态的，
    # 既会挡住「签到」按钮，又会被 find_panel 误当成签到弹窗（2026-11-19 00:07 实测）
    if not a.check:
        clear_ads(verbose=True)

    main_hwnd = _reuse_detail(a.detail_hwnd)
    if main_hwnd:
        print(f"\u267b\ufe0f 复用详情页窗口 hwnd={main_hwnd}（不做模板匹配，省 0.5s）")
    else:
        main_hwnd = find_platform()
    if not main_hwnd:
        print("❌ 没找到平台窗口")
        return 2
    reused = bool(a.detail_hwnd) and main_hwnd == a.detail_hwnd

    # 已经开着的弹窗（重复调用时）
    panel, psize = find_panel(main_hwnd, timeout=0.6)
    if panel is not None and _panel_kind(panel) is None:
        # 开着的这个窗既没有「立即签到」也没有「今日已签到」→ 多半是广告/活动小窗
        if not a.check and clear_ads(verbose=True):
            print("   ↻ 刚才那个是广告弹窗，已关掉 → 重新点「签到」")
            panel, psize = None, None
    if panel is None:
        small, k = snap1080(main_hwnd)
        if small is None:
            print("❌ 平台截图失败")
            return 3
        m = pg.match(small, TPL_SIGN, SIGN_SCORE)
        if reused and (not m or m[0] < SIGN_SCORE):
            # 复用的判断只是一次便宜的窗口存在性检查。万一下面这一秒里页面被切走了
            # （点了别的 tab、平台自己跳页），这里兜一次完整的形态校正，绝不再往下盲点。
            print("   ↻ 复用的窗口里没有「签到」按钮 → 重新做一次形态校正")
            try:
                import plat_home as ph
                ph.ensure_detail(verbose=True)
            except Exception as _e:
                print(f"   (形态校正失败：{_e!r})")
            h2 = find_platform()
            if h2:
                main_hwnd = h2
                reused = False
                small, k = snap1080(main_hwnd)
                if small is None:
                    print("❌ 平台截图失败")
                    return 3
                m = pg.match(small, TPL_SIGN, SIGN_SCORE)
        if m and m[0] >= SIGN_SCORE:
            bx, by = m[1]
            src = f"模板 {m[0]:.3f}"
            if m[0] < SIGN_SCORE + 0.06:
                # 2026-11-18 实测只有 0.810（阈值 0.80）——余量太薄，UI 一改就会掉到兜底坐标。
                # 这里不悄悄过，留一行日志方便以后定位。
                print(f"   ⚠️ 签到按钮模板分偏低（{m[0]:.3f}，阈值 {SIGN_SCORE}）→ UI 可能快变了，"
                      f"掉下去就会退回固定坐标 {SIGN_XY}")
        else:
            bx, by = SIGN_XY
            src = "兜底固定坐标"
            if m:
                print(f"   ⚠️ 签到按钮模板没命中（最高 {m[0]:.3f} < {SIGN_SCORE}）→ 用兜底坐标")
        print(f"🔎 签到按钮 1080 基准({bx},{by}) 来源={src}")
        if a.check:
            print("（--check 只算位置，不点击；弹窗状态未知）")
            return 0
        real = (bx / k, by / k)        # 1080 基准 → 真实客户区像素（要除缩小比）
        panel = None
        for attempt in range(1, 4):    # 页面还在加载/动画时点击会被吞，最多点 3 次
            click_client(main_hwnd, *real)
            print(f"👆 已点击签到按钮 客户区({real[0]:.0f},{real[1]:.0f})"
                  + ("" if attempt == 1 else f"（第 {attempt} 次）"))
            panel, psize = find_panel(main_hwnd, timeout=4.0)
            if panel is not None:
                break
            print("   ⏳ 没弹窗，0.8s 后重点一次")
            time.sleep(0.8)
        if panel is None:
            print("❌ 点了 3 次都没等到签到弹窗")
            return 4
    else:
        print(f"ℹ️ 弹窗已经开着 hwnd={panel} {psize}")

    img, tag, center = wait_ready(panel)
    save_img(os.path.join(a.shot, "sign_panel.png"), img)
    t_state = time.time()
    print(f"🪟 弹窗 hwnd={panel} {psize[0]}x{psize[1]} 状态={tag} 按钮中心={center}")

    if tag is None:
        print("❔ 弹窗里既不认识「立即签到」也不认识「今日已签到」，只存图不点")
        save_img(os.path.join(a.shot, "sign_unknown.png"), img)
        # 2026-11-19：以前这里直接返回 5，弹窗留在屏幕上挡住 ② 领券。
        # 现在既然已经确认它不是签到弹窗，就按用户教的关弹窗方式（X → ESC）收掉。
        if not a.check:
            print(f"🧹 收掉这个不认识的弹窗 → {'成功' if close_panel(panel) else '没关掉'}")
        return 5

    if tag == "done":
        print("✅ 今天已经签过了（今日已签到）")
        if not a.keep and not a.check:
            hold_then_close(panel, a.hold, t_state)
        return 0

    if a.check:
        print("ℹ️ 今日未签到（--check 不点）")
        return 0

    double_click_client(panel, center[0], center[1])
    t_dbl = time.time()
    print(f"👆👆 已双击「立即签到」 {center}")
    time.sleep(1.0)
    img2 = pg.grab_hwnd(panel)
    save_img(os.path.join(a.shot, "sign_reward.png"), img2)
    time.sleep(1.2)
    img3 = pg.grab_hwnd(panel)
    save_img(os.path.join(a.shot, "sign_after.png"), img3)
    tag3, _ = panel_state(img3)
    if tag3 == "done":
        print("✅ 签到成功（按钮已变「今日已签到」，第一天应显示「已领取」）")
        ok = True
    else:
        print("⚠️ 双击后没看到「今日已签到」，可能没点上，看截图 logs/sign_after.png")
        ok = False
    if not a.keep:
        hold_then_close(panel, a.hold, t_dbl)
    return 0 if ok else 6


if __name__ == "__main__":
    raise SystemExit(main())
