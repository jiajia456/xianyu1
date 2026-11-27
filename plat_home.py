# -*- coding: utf-8 -*-
"""平台形态守卫：确保有「地图详情页」窗口可用

用户 2026-11-18 13:56 明确指出的坑：
  打完一局点「退出游戏」后，屏幕停在**游戏房间**窗口（1224x904，有房间号/开始游戏/邀请/退出）。
  签到、领券的 find_platform() 只是「取面积最大的平台窗」，于是拿到这个房间窗，
  往里面找顶部「签到」按钮（房间里没有 → 兜底坐标 231,151 点在房间的地图封面上，连点 3 次全空）
  和 tab「任务」（房间里没有 → score 0.63 → 直接跳过领券）。
  实测 2026-11-18 13:33 那一轮就是这样整段【签到领券】白跑。

实测事实（2026-11-18 14:00）：
  平台是**双窗口**并存的 —— 主窗口（详情页 1332x945）和房间窗口（1224x904）标题都叫
  「KK官方对战平台」，谁在前台不一定。所以不能"取最大那个"碰运气，要按形态挑：
    ① 扫全部平台窗，各自判形态
    ② 有 detail（详情页）→ 直接用（PrintWindow 抓图不受遮挡影响）
    ③ 只剩 room（房间）→ 点「退出」退房，等主窗口回到详情页
    ④ other（任务页/大厅）→ 点一下「地图详情」tab

形态判据（实测分离度，阈值 0.85）：
  详情页：btn_create_room=0.905 / btn_quick_join=0.953
  房间  ：btn_create_room=0.412 / btn_quick_join=0.409
  房间另有 room_finder.is_room() 双保险：房间号 1.000 + 开始游戏 0.991

用法：
  python plat_home.py            # 检查形态；只在房间窗时退房回去
  python plat_home.py --check    # 只看现在是什么形态，不动手
  python plat_home.py --no-leave # 允许检测但不退房（房间形态直接判失败）
"""
from __future__ import annotations

import argparse
import os
import time

import cv2
import numpy as np
import win32gui

import popup_guard as pg

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_CREATE = os.path.join(HERE, "templates", "btn_create_room.png")
TPL_JOIN = os.path.join(HERE, "templates", "btn_quick_join.png")

BASE_W = 1080.0
DETAIL_SCORE = 0.85           # 详情页特征按钮阈值（房间上只有 0.41）
TAB_DETAIL_1080 = (166, 199)  # 「地图详情」tab（1080 基准，fishing_mode 已在用）
TPL_TAB = "tab_detail.png"    # 「地图详情」tab 截图（激活态蓝字+下划线），用来判"能不能安全点这排 tab"

FACE_CN = {"detail": "地图详情页", "room": "游戏房间", "other": "其它页面", "none": "抓不到画面"}


def _rd(path):
    """中文路径读模板（cv2.imread 读不了中文名）"""
    if not os.path.exists(path):
        return None
    try:
        return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def _best(img, paths):
    """多模板取最高分，返回 (score, (cx, cy))"""
    best, pt = 0.0, None
    for p in paths:
        tpl = _rd(p)
        if tpl is None or tpl.shape[0] > img.shape[0] or tpl.shape[1] > img.shape[1]:
            continue
        r = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx > best:
            best = float(mx)
            pt = (loc[0] + tpl.shape[1] // 2, loc[1] + tpl.shape[0] // 2)
    return best, pt


def face_of(img):
    """判断单个窗口截图是什么形态 → (名字, 明细)

    detail：详情页（有「创建房间」「快速加入」）
    room  ：房间（房间号 + 开始游戏）
    other ：其它页面（任务页 / 大厅 / 加载中…）
    """
    if img is None or not getattr(img, "size", 0):
        return "none", {}
    s_create, _ = _best(img, [TPL_CREATE])
    s_join, _ = _best(img, [TPL_JOIN])
    info = {"create": round(s_create, 3), "join": round(s_join, 3)}
    # 2026-11-19 降 CPU：**先判详情页**。
    # 详情页是两个蓝色按钮（创建房间 / 快速加入 ≥0.85），房间窗上这两个分只有
    # 0.412 / 0.409（实测），分离度很大；命中就直接返回，省掉 is_room 里
    # 房间号×2 + 开始游戏×3 共 5 个模板匹配（实测 0.344s/次，占 face_of 七成）。
    # 顺序调换对判定结果没有影响：房间分支要求「房间号≥0.90 且 开始游戏≥0.72」，
    # 与详情页判据不会同时成立（一个窗口真要同时满足，才轮到这里的先后之分）。
    if s_create >= DETAIL_SCORE or s_join >= DETAIL_SCORE:
        return "detail", info
    # 再判房间：房间号是强特征，不会被详情页的蓝色按钮带偏
    try:
        import room_finder as rf
        is_r, s_no, s_start = rf.is_room(img)
        info["roomno"] = round(float(s_no), 3)
        info["start"] = round(float(s_start), 3)
        if is_r:
            return "room", info
    except Exception as e:
        info["is_room_err"] = repr(e)
    return "other", info


def scan(verbose=False):
    """扫全部平台窗口，逐个判形态 → [{'hwnd','size','face','info','img'}]"""
    rows = []
    for hwnd, title, size in pg.enum_candidates(False):
        img = pg.grab_hwnd(hwnd)
        f, info = face_of(img)
        rows.append({"hwnd": hwnd, "size": size, "face": f, "info": info, "img": img})
        if verbose:
            print("      · hwnd=%d %dx%d → %s %s"
                  % (hwnd, size[0], size[1], FACE_CN.get(f, f), info), flush=True)
    return rows


def pick(rows, face):
    """从扫描结果里挑某形态的窗口（多个取面积最大）"""
    hit = [r for r in rows if r["face"] == face]
    if not hit:
        return None
    hit.sort(key=lambda r: -r["size"][0] * r["size"][1])
    return hit[0]


def find_platform():
    """兼容旧调用：返回详情页窗口优先，其次最大平台窗"""
    rows = scan()
    for f in ("detail", "other", "room"):
        r = pick(rows, f)
        if r:
            return r["hwnd"]
    return None


def platform_pids():
    """平台进程 pid 集合（用可见平台窗反查）"""
    import win32process
    pids = set()
    for hwnd, title, size in pg.enum_candidates(False):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pids.add(pid)
        except Exception:
            pass
    return pids


def revive():
    """把被隐藏/最小化的平台主窗口（地图详情页）叫出来。

    真实场景：点「开始游戏」后主窗口可能被隐藏/最小化，只剩房间窗可见。

    ❗ 三个坑（实测 2026-11-18 14:03）：
      ① 最小化窗口 GetClientRect = 0x0
      ② 最小化窗口 GetWindowRect 也只有 160x28（只剩标题栏）
         → 任何“按尺寸挑”都会把主窗口漏掉，反而挑到房间窗
      ③ 用可见窗反查 pid 再按 pid 过滤也靠不住
    所以正确做法：枚举同名窗口 → 已最小化的先 RESTORE → 截图判形态 →
    是「地图详情页」就返回它。
    """
    cands = []

    def cb(h, _):
        t = win32gui.GetWindowText(h) or ""
        low = t.lower()
        if "对战平台" not in t and not low.startswith("kk"):
            return
        if win32gui.IsIconic(h):
            cands.append(h)                    # 最小化的全收（尺寸不可信）
            return
        r = win32gui.GetWindowRect(h)
        if r[2] - r[0] < 400 or r[3] - r[1] < 400:
            return
        cands.append(h)

    win32gui.EnumWindows(cb, None)
    if not cands:
        return None

    # 已在台面上的先试（不用动它），最小化的放后面再还原
    cands.sort(key=lambda h: 0 if not win32gui.IsIconic(h) else 1)
    for h in cands:
        was_min = bool(win32gui.IsIconic(h))
        if was_min:
            try:
                win32gui.ShowWindow(h, 9)      # SW_RESTORE
            except Exception:
                pass
            time.sleep(0.45)
        f, _ = face_of(pg.grab_hwnd(h))
        if f != "detail":
            time.sleep(0.35)
            f, _ = face_of(pg.grab_hwnd(h))     # 刚还原时还在重绘，再等一次
        if f == "detail":
            try:
                win32gui.SetForegroundWindow(h)
            except Exception:
                pass
            return h
        if was_min:
            # 不是详情页又被我们还原了：保持原样放回最小化，别改用户布局
            try:
                win32gui.ShowWindow(h, 6)
            except Exception:
                pass
    return None


def click_tab_detail(hwnd, verbose=False):
    """点一下「地图详情」tab（把 任务页 拉回来）——**必须先认出 tab 才点**

    2026-11-18 15:30 加闸门：老版本无条件点 (166,199)@1080 基准。平台停在**游戏大厅**时，
    这个坐标压在首页「黑曜SVIP会员卡」充值横幅上，一点就跳支付/活动页。
    所以现在先在该位置找 `tab_detail.png`（「地图详情」四个字），认不到就返回 None、绝不点。
    真正从大厅回详情页走 go_detail_by_search()。
    """
    r = win32gui.GetClientRect(hwnd)
    if r[2] <= 0 or r[3] <= 0:
        return None
    img = pg.grab_hwnd(hwnd)
    if img is None or not getattr(img, "size", 0):
        return None
    h, w = img.shape[:2]
    k = w / BASE_W
    # 在 tab 区域附近（放宽一圈）找模板
    x0, y0 = int(TAB_DETAIL_1080[0] * k) - 10, int(TAB_DETAIL_1080[1] * k) - 30
    x1, y1 = x0 + 200, y0 + 90
    roi = img[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
    sc = 0.0
    try:
        import cv2
        tpl = _rd(os.path.join(HERE, "templates", TPL_TAB))
        if tpl is not None and roi.size:
            best = 0.0
            for s in (0.9, 1.0, 1.1, 1.2, 1.25):
                tt = cv2.resize(tpl, None, fx=s, fy=s) if abs(s - 1.0) > 1e-6 else tpl
                if tt.shape[0] > roi.shape[0] or tt.shape[1] > roi.shape[1]:
                    continue
                rr = cv2.matchTemplate(roi, tt, cv2.TM_CCOEFF_NORMED)
                best = max(best, float(cv2.minMaxLoc(rr)[1]))
            sc = best
    except Exception:
        return None
    if sc < 0.75:
        if verbose:
            print("   ↳ tab 位置没认出「地图详情」（score=%.3f）→ 不点，改用搜地图" % sc,
                  flush=True)
        return None
    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    sx = int(ox + TAB_DETAIL_1080[0] * k)
    sy = int(oy + TAB_DETAIL_1080[1] * k)
    pg.click_screen(hwnd, sx, sy)
    if verbose:
        print("   ↳ 认出「地图详情」tab（score=%.3f）→ 点一下" % sc, flush=True)
    return sx, sy


def go_detail_by_search(verbose=True, timeout=120):
    """从「游戏大厅」等页面切到地图详情页：走「搜地图 → 双击封面」这条确定的路

    比盲点 tab 安全得多：大厅首页左侧那个位置是充值横幅。
    返回 True/False（子进程 exit code == 0）。
    """
    import subprocess
    import sys
    say = (lambda x: print(x, flush=True)) if verbose else (lambda x: None)
    say("   ↳ 没有「地图详情」tab 可点（多半停在游戏大厅）→ 用「搜地图」切过去")
    try:
        r = subprocess.run([sys.executable, "game_auto.py", "run",
                            "--config", "configs/wuxia.json",
                            "--task", "search_double", "--autostart"],
                           cwd=HERE, timeout=timeout)
        say("   ↳ 搜地图结束 exit=%s" % r.returncode)
        return r.returncode == 0
    except Exception as e:
        say("   ❌ 搜地图失败：%r" % (e,))
        return False


def ensure_detail(verbose=True, timeout=45.0, allow_leave=True, dry=False):
    """确保有一个「地图详情页」窗口可用。

    返回 dict：
      ok      True 表示详情页窗口存在（本来就在，或退房后回来了）
      reason  already_detail / back_to_detail / dry_<face> / no_platform_window /
              leave_failed_<r> / no_leave_allowed / timeout_<face>
      face    当前形态
      info    各判据得分
      hwnd    详情页窗口句柄（ok=True 时有值）
      rooms   扫描时看到的房间窗口 hwnd 列表
    """
    pg.enable_dpi_awareness()
    say = (lambda s: print(s, flush=True)) if verbose else (lambda s: None)

    rows = scan(verbose=verbose)
    if not rows:
        say("   ❌ 没找到平台窗口")
        return {"ok": False, "reason": "no_platform_window", "face": None,
                "info": {}, "hwnd": None, "rooms": []}

    det = pick(rows, "detail")
    rooms = [r["hwnd"] for r in rows if r["face"] == "room"]
    if det:
        say("   ↳ 平台形态：%s hwnd=%d %dx%d %s"
            % (FACE_CN["detail"], det["hwnd"], det["size"][0], det["size"][1], det["info"]))
        return {"ok": True, "reason": "already_detail", "face": "detail",
                "info": det["info"], "hwnd": det["hwnd"], "rooms": rooms}

    # 先试一次：主窗口可能只是被隐藏/最小化了（点「开始游戏」后常见）。
    # 能叫回来就别退房 —— 退房会丢掉房间，下一局还得重建。
    if not dry:
        rv = revive()
        if rv:
            say("   ↳ 主窗口不在可见列表（被隐藏/最小化），已还原 hwnd=%d" % rv)
            time.sleep(0.9)
            rows = scan(verbose=verbose)
            det2 = pick(rows, "detail")
            rooms = [r["hwnd"] for r in rows if r["face"] == "room"]
            if det2:
                say("   ✅ 主窗口还原后就是详情页 hwnd=%d %dx%d（未退房）"
                    % (det2["hwnd"], det2["size"][0], det2["size"][1]))
                return {"ok": True, "reason": "already_detail_restored", "face": "detail",
                        "info": det2["info"], "hwnd": det2["hwnd"], "rooms": rooms}

    main = pick(rows, "room") or pick(rows, "other") or rows[0]
    f, info = main["face"], main["info"]
    say("   ↳ 平台只有一个窗口，形态：%s hwnd=%d %dx%d %s"
        % (FACE_CN.get(f, f), main["hwnd"], main["size"][0], main["size"][1], info))
    if dry:
        return {"ok": False, "reason": "dry_%s" % f, "face": f, "info": info,
                "hwnd": main["hwnd"], "rooms": rooms}

    left = None
    if f == "room":
        if not allow_leave:
            say("   ⚠️ 平台停在房间窗口，但被禁止退房（--no-leave）")
            return {"ok": False, "reason": "no_leave_allowed", "face": f, "info": info,
                    "hwnd": main["hwnd"], "rooms": rooms}
        import room_finder as rf
        say("   ↳ 房间窗口里没有「签到」「任务」→ 先点「退出」回详情页")
        left = rf.leave_room(hwnd=main["hwnd"], verbose=verbose, tries=2)
        say("   ↳ 退出房间结果：%s" % left)
        if left == "no_quit_btn":
            return {"ok": False, "reason": "leave_failed_no_quit_btn", "face": f,
                    "info": info, "hwnd": main["hwnd"], "rooms": rooms}
        time.sleep(1.5)

    # 等详情页窗口出现：连续 2 次命中才算（刚退房/刚点 tab 时页面还在重绘）
    t0 = time.time()
    hit = 0
    searched = False          # 「搜地图」兜底只做一次，避免来回跳
    last_f, last_info, last_hwnd = f, info, main["hwnd"]
    last_click = 0.0
    while time.time() - t0 < timeout:
        rows2 = scan()
        d2 = pick(rows2, "detail")
        if d2:
            hit += 1
            if hit >= 2:
                say("   ✅ 详情页已就绪 hwnd=%d %dx%d（%.1fs）"
                    % (d2["hwnd"], d2["size"][0], d2["size"][1], time.time() - t0))
                return {"ok": True, "reason": "back_to_detail", "face": "detail",
                        "info": d2["info"], "hwnd": d2["hwnd"],
                        "rooms": [r["hwnd"] for r in rows2 if r["face"] == "room"],
                        "left": left}
        else:
            hit = 0
            now = time.time()
            if now - last_click > 3.0:
                last_click = now
                # 先把被隐藏/最小化的主窗口叫出来
                rv = revive()
                if rv:
                    say("   ↳ 主窗口不在可见列表里，已还原 hwnd=%d" % rv)
                o2 = pick(rows2, "other") or pick(rows2, "room")
                if o2:
                    last_f, last_info, last_hwnd = o2["face"], o2["info"], o2["hwnd"]
                    if o2["face"] == "other":
                        pt = click_tab_detail(o2["hwnd"], verbose=verbose)
                        if pt is None and not searched:
                            searched = True
                            go_detail_by_search(verbose=verbose)
        time.sleep(1.0)

    say("   ⚠️ 等了 %.0fs 还没有详情页（当前：%s）" % (timeout, FACE_CN.get(last_f, last_f)))
    return {"ok": False, "reason": "timeout_%s" % last_f, "face": last_f,
            "info": last_info, "hwnd": None, "rooms": rooms, "left": left}


def main():
    ap = argparse.ArgumentParser(description="平台形态守卫：确保有「地图详情页」窗口")
    ap.add_argument("--check", action="store_true", help="只看形态，不动手")
    ap.add_argument("--no-leave", action="store_true", help="允许检测但不退房")
    ap.add_argument("--timeout", type=float, default=45.0)
    a = ap.parse_args()
    r = ensure_detail(verbose=True, timeout=a.timeout, allow_leave=not a.no_leave, dry=a.check)
    print("结果：ok=%s reason=%s face=%s hwnd=%s rooms=%s"
          % (r["ok"], r["reason"], r["face"], r["hwnd"], r.get("rooms")))
    return 0 if r["ok"] else (3 if a.check else 4)


if __name__ == "__main__":
    raise SystemExit(main())
