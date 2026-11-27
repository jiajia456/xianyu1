# -*- coding: utf-8 -*-
"""【钓鱼模式】标准流程（用户 2026-11-17 01:35 定义，01:39 定名）

四步，全程不需要任何局内操作：
  1) 开局到大厅（选难度界面）  game_auto.py pipeline --only ①,②,③,④（跳过 ⑤ 选难度）
  2) 等大厅渲染出「钓鱼模式」横幅 → 模板定位 → 悬停 1s 单击 → 校验是否真进去了
     （模板 templates/tpl_fishing_banner.png；画面先归一化到 1690×921 再匹配，
       游戏刚启动时窗口是 1264×921，直接按比例换算会点空 —— 14:33 实测）
     （进入判据 templates/tpl_fishing_scene.png 命中「当局钓鱼积分：」字样，阈值 0.78）
  3) 安静等待 N 小时（默认 5）  —— 期间不截图、不抢焦点、不做任何操作
  4) 退出游戏（F10→E→X→X），校验进程，必要时 kill_war3()

用法：
  python fishing_mode.py                    # 默认等 5 小时
  python fishing_mode.py --hours 0.1        # 等 6 分钟（调试）
  python fishing_mode.py --enter-only       # 只进钓鱼，不进等待（看进没进对）
  python fishing_mode.py --no-exit          # 等完不退出（留着现场）
  python fishing_mode.py --leave-room       # 结束后顺手退房间回平台（默认不退，保留房间给下一轮复用）

退出码：0 正常；2 平台/游戏窗口异常；3 开局失败；4 没找到钓鱼入口；5 退出失败
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 钓鱼横幅：客户区基准尺寸 1690×921 上的绿字中心
BASE_W, BASE_H = 1690, 921
FISH_XY = (1142, 767)          # 老坐标（模板命中过，留着当参考）
BANNER_XY = (1090, 775)        # 「钓鱼模式」横幅中心实测（1690x921 基准，2026-11-18 14:24）


def stamp(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(args, label, timeout=None):
    cmd = [sys.executable, *args]
    stamp(f"▶ {label}")
    try:
        r = subprocess.run(cmd, cwd=HERE, timeout=timeout)
        return r.returncode
    except subprocess.TimeoutExpired:
        stamp(f"⏱ {label} 超时")
        return 124


def war3_alive():
    try:
        import full_loop as fl
        return bool(fl.find_war3())
    except Exception:
        return False


def ensure_detail_page():
    """开局前确认平台有可用页面（详情页 / 房间窗都行），**不再盲点 tab**。

    2026-11-18 14:20 改：老版本无条件点 (166,199)（1080 基准）想切「地图详情」tab，
    但平台如果停在「游戏大厅」，那个坐标正好压在首页「黑曜SVIP会员卡」充值横幅上，
    一点就跳支付/活动页 —— 开局全毁。所以现在改成先判形态：
      detail（详情页/任务页，底部有创建房间按钮）→ 直接用
      room  （游戏房间）→ 直接用，pipeline 的 skip_if_room 会跳过 ①②③ 直接 ④
      other （游戏大厅等）→ 什么都不点，交给 ① 搜索地图（搜索框全平台通用）自己切过去
    页面渲染慢的问题改由 pipeline ① 的 settle 加长来兜。
    """
    try:
        import plat_home as ph
        r = ph.ensure_detail(verbose=True, dry=True)
    except Exception as e:
        stamp(f"(形态检查失败：{e}) → 交给 ① 自己切页")
        return True
    face = r.get("face")
    if face in ("detail", "room"):
        stamp(f"\U0001F3E0 平台形态 {face} → 可直接开局")
    else:
        stamp(f"\U0001F3E0 平台在「{face}」页 → 不盲点 tab，交给 \u2460 搜索地图切页")
    return True


TPL_FISH_BANNER = "tpl_fishing_banner.png"


def _norm(img):
    """把画面统一缩放到基准 1690x921（游戏窗口会在 1264x921 / 1690x921 之间变）"""
    import cv2
    if img is None or not getattr(img, "size", 0):
        return None
    h, w = img.shape[:2]
    if (w, h) == (BASE_W, BASE_H):
        return img
    return cv2.resize(img, (BASE_W, BASE_H))


def _match(img, tpl_path, scales=(1.0,), thr=0.78):
    """在（已归一化的）画面里找模板 → (cx, cy, score) 或 None"""
    if img is None or not os.path.exists(tpl_path):
        return None
    try:
        import cv2
        import numpy as np
        t = cv2.imdecode(np.fromfile(tpl_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if t is None:
            return None
        best, bpt = 0.0, None
        for sc in scales:
            tt = cv2.resize(t, None, fx=sc, fy=sc) if abs(sc - 1.0) > 1e-6 else t
            if tt.shape[0] > img.shape[0] or tt.shape[1] > img.shape[1]:
                continue
            r = cv2.matchTemplate(img, tt, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(r)
            if mx > best:
                best, bpt = float(mx), (loc[0] + tt.shape[1] // 2,
                                        loc[1] + tt.shape[0] // 2)
        if bpt is None or best < thr:
            return None
        return bpt[0], bpt[1], best
    except Exception:
        return None


def find_fishing_banner(img):
    """在大厅画面里找「钓鱼模式」横幅 → (x, y, score)，坐标已换算回原图

    客户区基准 1690x921 上横幅中心实测 (1090, 775)。
    ⚠️ 14:33 踩坑：游戏刚启动时窗口是 1264x921（还在加载画面），
    直接按 client_rect 换算会点空；而把 1690 基准的模板拿去 1264 宽的画面里找也找不到
    （缩放比 0.748 不在搜索范围）。所以先把画面归一化到 1690x921 再匹配，最后换算回去。
    """
    n = _norm(img)
    hit = _match(n, os.path.join(HERE, "templates", TPL_FISH_BANNER))
    if not hit:
        return None
    cx, cy, sc = hit
    h, w = img.shape[:2]
    return int(cx * w / BASE_W), int(cy * h / BASE_H), sc


def grab_win(hwnd):
    """抓窗口画面（PrintWindow，不抢焦点、不怕被遮挡）"""
    try:
        import popup_guard as pg
        return pg.grab_hwnd(hwnd)
    except Exception:
        return None


def save_shot(img, name):
    """存一张现场截图到 logs/，返回路径（写不进去就退临时目录）

    注意：`cv2.imwrite` 在中文路径下会静默失败（项目目录名带「我在峡谷打」），
    所以统一走 `imencode + ndarray.tofile`，这样才能真的落进 logs/ 而不是每次都退化到 %TEMP%。
    """
    try:
        import cv2
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return None
        for d in (os.path.join(HERE, "logs"),
                  os.path.join(os.environ.get("TEMP", "."), "KKAuto", "logs")):
            try:
                os.makedirs(d, exist_ok=True)
                fp = os.path.join(d, name)
                buf.tofile(fp)
                return fp
            except Exception:
                continue
    except Exception:
        pass
    return None


FISH_SCENE_THR = 0.78


def fishing_score(img):
    """钓鱼场景模板分（没模板 / 图空 / 分数不够 → None）

    ⚠️ 14:44 踩坑：这里原来 thr=0.0，任何画面都能"命中"一个 0.x 的分数，
    调用方用 `sc is not None` 判断 → 加载画面/大厅都被当成"已经在钓鱼"，
    于是横幅根本没点，白等 6 分钟。必须卡阈值（正样本 1.000 / 大厅 0.518）。
    """
    n = _norm(img)
    hit = _match(n, os.path.join(HERE, "templates", "tpl_fishing_scene.png"),
                 scales=(0.95, 1.0, 1.05), thr=FISH_SCENE_THR)
    return None if not hit else hit[2]


def wait_lobby_banner(win, timeout=150.0, gap=4.0):
    """等大厅真正渲染出来（能认到钓鱼横幅）→ (x, y, score) 或 None

    为什么要等：14:33 那次是加载画面还没过去就点了，点在加载画面上 = 白点，
    而且「横幅消失」的校验也失效（模板本来就没命中）→ 假报成功。
    """
    import time as _t
    t0 = _t.time()
    last = 0.0
    while _t.time() - t0 < timeout:
        img = grab_win(win.hwnd)
        pt = find_fishing_banner(img)
        if pt:
            return pt
        sc = fishing_score(img)
        if sc is not None:
            return None          # 已经是钓鱼场景了（不用点）
        if _t.time() - last > 20:
            last = _t.time()
            size = img.shape[1::-1] if (img is not None and getattr(img, "size", 0)) else "?"
            stamp(f"   …等大厅渲染（画面 {size}，已等 {int(_t.time() - t0)}s）")
        _t.sleep(gap)
    return None


def wait_fishing_scene(win, timeout=90.0):
    """点完横幅后确认到底进没进钓鱼场景 → (状态, 截图路径)

    状态：True 进对了 / False 没进去 / None 判不了（还没做场景模板，先存证截图）
    """
    import time as _t
    t0 = _t.time()
    shot = None
    while _t.time() - t0 < timeout:
        img = grab_win(win.hwnd)
        sc = fishing_score(img)
        if sc is None:
            _t.sleep(25)                       # 没模板：等场景切完，截图存证
            now = grab_win(win.hwnd)
            shot = save_shot(now if now is not None else img,
                             "fishing_enter_%s.png" % _t.strftime("%H%M%S"))
            return None, shot
        if sc >= 0.80:
            shot = save_shot(img, "fishing_enter_%s.png" % _t.strftime("%H%M%S"))
            return True, shot
        _t.sleep(4)
    shot = save_shot(grab_win(win.hwnd),
                     "fishing_fail_%s.png" % _t.strftime("%H%M%S"))
    return False, shot


def enter_fishing():
    """步骤 1+2：pipeline 到大厅 → 点钓鱼横幅 → 返回 True/False"""
    import game_auto as ga

    # DPI 感知必须先开（鼠标走物理像素）
    ga.enable_dpi_awareness()

    # 1) 开局到大厅
    rc = run(["game_auto.py", "pipeline", "--pipeline", "configs/pipeline.json",
              "--only", "①,②,③,④"], "①-1 开局到大厅（跳过 ⑤ 选难度）", timeout=600)
    if rc != 0:
        stamp(f"❌ 开局失败 exit={rc}")
        return False

    # 2) 等 War3 窗口（④ 开局后游戏要几十秒才起来）
    win = None
    t0 = time.time()
    while time.time() - t0 < 120:
        try:
            win = ga.GameWindow(title_re=r"Warcraft", pick="largest")
            break
        except Exception:
            time.sleep(2.0)
    if win is None:
        stamp("❌ 等了 120s 没等到 War3 窗口")
        return False
    stamp(f"🪟 绑定 War3 hwnd={win.hwnd}")

    # 3) 等大厅渲染完（能认到「钓鱼模式」横幅）再动手
    pt = wait_lobby_banner(win, timeout=150)
    if pt is None:
        if fishing_score(grab_win(win.hwnd)) is not None:
            stamp("✅ 已经不在大厅（像是钓鱼场景）→ 直接等")
            ok, shot = wait_fishing_scene(win, timeout=60)
            if shot:
                stamp(f"📷 现场截图：{shot}")
            return True
        img = grab_win(win.hwnd)
        ww, hh = (img.shape[1], img.shape[0]) if (img is not None
                                                 and getattr(img, "size", 0)) else (BASE_W, BASE_H)
        stamp("⚠️ 150s 内没认到「钓鱼模式」横幅 → 只能按基准坐标盲点一次")
        pt = (int(BANNER_XY[0] * ww / BASE_W), int(BANNER_XY[1] * hh / BASE_H), 0.0)

    # 4) 点横幅（War3 必须先悬停再点），点完校验横幅是否消失，没消失就再点
    inp = ga.Input(win)
    gone = False
    for attempt in range(1, 4):
        x, y, sc = pt
        stamp(f"🎣 钓鱼横幅 score={sc:.3f} @({x},{y}) 第 {attempt} 次点击")
        inp.click_hover(int(x), int(y), hover=1.0, clicks=1)
        time.sleep(6)
        if fishing_score(grab_win(win.hwnd)) is not None:
            gone = True
            stamp("✅ 已离开大厅（进钓鱼场景了）")
            break
        pt2 = find_fishing_banner(grab_win(win.hwnd))
        if pt2 is None:
            gone = True
            stamp("✅ 大厅横幅已消失（已切场景）")
            break
        pt = pt2
        stamp(f"⚠️ 第 {attempt} 次点击后横幅还在 → 换个位置再点")

    # 5) 确认场景；还没走就等底部倒计时自动进（约 300s）
    ok, shot = wait_fishing_scene(win, timeout=90)
    if shot:
        stamp(f"📷 现场截图：{shot}")
    if ok is True:
        stamp("✅ 已确认进入钓鱼场景")
    elif ok is False:
        if gone:
            stamp("⚠️ 已离开大厅但没认到钓鱼场景模板（补拍截图，等人工确认）")
        else:
            stamp("⚠️ 还没进 → 等底部倒计时自动进（最多 5 分钟）")
            t1 = time.time()
            while time.time() - t1 < 300:
                time.sleep(10)
                if fishing_score(grab_win(win.hwnd)) is not None:
                    stamp("✅ 倒计时自动进钓鱼了")
                    break
    else:
        if gone:
            stamp("✅ 已离开大厅（钓鱼场景暂无模板可校验，截图已存证）")
        else:
            stamp("⚠️ 横幅还在，可能没进；等底部约 300s 倒计时自动进")
    return True


def login_abort(where=""):
    """检测到登录窗口 / 账号被顶下线 → 停手（不退出游戏、不关窗口、不点登录/确定）"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        import login_guard as lg
        rsn = lg.last_reason()
    except Exception:
        rsn = "检测到登录窗口"
    stamp("=" * 56)
    stamp(f"⛔ {rsn}：{ts}")
    stamp(f"   位置：{where or '钓鱼模式'}")
    stamp("   已停止任务，未做任何操作（没点登录/确定、没关窗口、没退游戏）")
    stamp("   记录：logs/login_events.jsonl")
    stamp("=" * 56)
    return 131


def check_login(label=""):
    try:
        import login_guard as lg
    except Exception:
        return None
    return lg.check(label or "钓鱼模式")


def wait_hours(hours, chunk=600):
    """安静等待；每 60s 顺手扫一眼登录窗口（只看不动，命中立刻停手）"""
    total = int(round(hours * 3600))
    t0 = time.time()
    stamp(f"😴 安静等待 {hours:g} 小时（{total}s），期间不截图、不操作")
    # 2026-11-23 00:40 用户要求：War3 检查每 3 分钟；登录窗口检查也改为每 3 分钟
    last_war3_check = time.time()
    last_login_check = time.time()
    while True:
        left = total - (time.time() - t0)
        if left <= 0:
            break
        # 60s 一小段（只用于刷新倒计时显示）
        step = min(60, chunk, left)
        time.sleep(step)
        if time.time() - last_login_check >= 180:
            last_login_check = time.time()
            if check_login("钓鱼等待期间"):
                return 131
        if time.time() - last_war3_check >= 180:
            last_war3_check = time.time()
            if not war3_alive():
                stamp("⛔ 钓鱼期间 War3 已退出（窗口不见了）→ 中断等待")
                return 3
            stamp("   🎮 War3 进程正常（每 3 分钟检查）")
        left_s = max(0, int(total - (time.time() - t0)))
        stamp(f"   ⏳ 还剩 {left_s // 60} 分 {left_s % 60} 秒")
    stamp("✅ 等待结束")
    return 0


def quit_game():
    rc = run(["game_auto.py", "run", "--config", "configs/war3_exit.json",
              "--task", "quit_game", "--autostart"], "④ 退出游戏", timeout=180)
    time.sleep(1.5)
    if war3_alive():
        stamp("⚠️ 正常退出没成功 → 直接杀进程")
        try:
            import cheat_guard as cg
            cg.kill_war3()
        except Exception as e:
            stamp(f"   (杀进程失败：{e})")
        time.sleep(2.0)
    ok = not war3_alive()
    stamp("✅ War3 已退出" if ok else "❌ War3 还在")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=5.0, help="钓鱼等待小时数（默认 5）")
    ap.add_argument("--enter-only", action="store_true", help="只进钓鱼不等待")
    ap.add_argument("--no-exit", action="store_true", help="等完不退出游戏")
    ap.add_argument("--leave-room", action="store_true", help="结束后退房间回平台（默认保留房间）")
    a = ap.parse_args()

    stamp("=" * 56)
    stamp(f"【钓鱼模式】开始  hours={a.hours:g}")
    stamp("=" * 56)
    if a.hours > 7.0:
        # 地图右上角常驻「7小时20分 自动退出游戏」（实测 2026-11-18 14:36 钓鱼场景）
        stamp("⚠️ 地图自带「7小时20分 自动退出游戏」→ 等待超过 7 小时可能被游戏自己踢出")
        stamp("   建议 hours 设 6 以内；被踢出后本脚本的退出步骤会自动跳过（War3 已不在）")

    # 0) 清场：上一轮 War3 没退干净先退出
    if check_login("钓鱼模式开始前"):
        return login_abort("钓鱼模式开始前")
    if war3_alive():
        stamp("⚠️ 发现残留 War3 → 先退出")
        quit_game()

    ensure_detail_page()

    if not enter_fishing():
        return 3
    if a.enter_only:
        stamp("（--enter-only 到此为止）")
        return 0

    if wait_hours(a.hours) == 131:
        return login_abort("钓鱼等待期间")

    if check_login("退出游戏前"):
        return login_abort("退出游戏前")

    if a.no_exit:
        stamp("（--no-exit 不退出游戏）")
    else:
        if not quit_game():
            return 5

    if a.leave_room:
        try:
            import room_finder as rf
            ok = rf.leave_room(verbose=True)
            stamp(f"{'✅' if ok else '⚠️'} 退出房间 → {'成功' if ok else '没退掉'}")
        except Exception as e:
            stamp(f"(退房失败：{e})")

    stamp("【钓鱼模式】结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
