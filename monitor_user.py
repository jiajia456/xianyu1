# -*- coding: utf-8 -*-
"""旁路监控「用户手动操作」——只读，绝不点击、不抢焦点。

用途：脚本点不开「秘境挑战」而人手能点开时，靠它把真实操作录下来，
      再和脚本的坐标/时序逐条对照，看差在哪。

记录内容
  · 每次鼠标左/右键：屏幕坐标、游戏客户区坐标、悬停时长、点前帧 + 点后 3 帧
  · 每次按键：键名 + 修饰键（Ctrl/Alt/Shift）
  · 无操作的画面突变：也存一帧（抓「面板闪一下就没了」这类情况）

输出
  logs/user_trace/trace.txt      人读的时间线
  logs/user_trace/events.jsonl   机读
  logs/user_trace/eNNN_*.png     事件快照

用法
  python monitor_user.py --secs 600
  python monitor_user.py --secs 600 --diff 5.0     # 更敏感的画面突变阈值
"""
import argparse
import ctypes
import json
import os
import time
from ctypes import wintypes

import cv2
import numpy as np
from PIL import ImageGrab

import game_auto as ga
import wave_auto as wv

user32 = ctypes.windll.user32

VK_NAME = {
    0x01: "鼠标左键", 0x02: "鼠标右键", 0x04: "鼠标中键",
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x20: "Space",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2D: "Insert", 0x2E: "Delete", 0x24: "Home", 0x23: "End",
    0x21: "PgUp", 0x22: "PgDn",
}
for _i in range(10):
    VK_NAME[0x30 + _i] = str(_i)
for _i in range(26):
    VK_NAME[0x41 + _i] = chr(0x41 + _i)
for _i in range(12):
    VK_NAME[0x70 + _i] = f"F{_i + 1}"
VK_NAME[0x6A] = "Num*"
VK_NAME[0x6B] = "Num+"
VK_NAME[0x6D] = "Num-"
VK_NAME[0x6E] = "Num."
VK_NAME[0x6F] = "Num/"

OUT_ROOT = os.path.join("logs", "user_trace")
OUT = OUT_ROOT          # main() 里换成 run_时间戳/ 子目录


def png_write(path: str, img: np.ndarray):
    """写 PNG。失败不能影响录制（本环境覆盖既有文件会被拒）。"""
    if img is None:
        return None
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    root, ext = os.path.splitext(path)
    for p in (path, "%s_%s%s" % (root, time.strftime("%H%M%S"), ext or ".png")):
        try:
            ok, buf = cv2.imencode(".png", img)
            if ok:
                buf.tofile(p)
                return p
        except Exception:
            continue
    return None


def raw_grab(w):
    """只截客户区，**不**像 in_game.grab 那样抢焦点（会打断用户操作）。"""
    cx, cy, cw, ch = w.client_rect()
    if cw <= 0 or ch <= 0:
        return None
    try:
        arr = np.array(ImageGrab.grab(bbox=(cx, cy, cx + cw, cy + ch)))
    except Exception:
        return None
    if arr.size == 0 or arr.shape[0] < 10:
        return None
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def fg_title() -> str:
    h = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value


def mods() -> str:
    s = []
    if user32.GetAsyncKeyState(0x11) & 0x8000:
        s.append("Ctrl")
    if user32.GetAsyncKeyState(0x12) & 0x8000:
        s.append("Alt")
    if user32.GetAsyncKeyState(0x10) & 0x8000:
        s.append("Shift")
    return "+".join(s)


def watch_vks():
    return [vk for vk in VK_NAME if 0x01 <= vk <= 0xFE]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=600, help="录多久（秒）")
    ap.add_argument("--diff", type=float, default=6.0, help="画面突变阈值（平均绝对差）")
    ap.add_argument("--baseline", type=float, default=0.30, help="基线帧刷新间隔")
    ap.add_argument("--keys", action="store_true", help="也记录键盘（默认只记鼠标）")
    ap.add_argument("--no-shot", action="store_true", help="只记坐标不存图")
    ap.add_argument("--wait-game", type=float, default=0,
                    help="等 War3 出现的最长秒数（游戏还没开就先等着）")
    args = ap.parse_args()

    # 每次录一份独立目录（run_YYYYmmdd_HHMMSS）：宿主机对「已存在的文件」拒绝覆盖
    # （open(..., "w") 会 PermissionError: Errno 13），删也删不掉（trash 失败），
    # 所以不要复用同一个 trace.txt，直接换目录，旧记录也不会被冲掉。
    global OUT
    stamp = time.strftime("%Y%m%d_%H%M%S")
    OUT = os.path.join(OUT_ROOT, "run_" + stamp)
    os.makedirs(OUT, exist_ok=True)

    w = None
    if args.wait_game > 0:
        deadline = time.time() + args.wait_game
        print(f"等 War3 窗口出现（最多 {args.wait_game:.0f}s）…", flush=True)
        while time.time() < deadline:
            w = wv.bind()
            if w is not None:
                break
            time.sleep(1.0)
    else:
        w = wv.bind()
    if w is None:
        print("❌ 没有 War3 窗口，先起游戏再录", flush=True)
        return 2
    print(f"绑定 War3 hwnd={w.hwnd} pid={w.pid} 客户区={w.client_rect()}", flush=True)
    print(f"录制 {args.secs:.0f}s → {OUT}/  （只读，不会点击你）", flush=True)
    print("开始操作吧。", flush=True)

    fg = open(os.path.join(OUT, "trace.txt"), "w", encoding="utf-8")
    fj = open(os.path.join(OUT, "events.jsonl"), "w", encoding="utf-8")

    vks = watch_vks()
    prev = {vk: False for vk in vks}

    t0 = time.time()
    base = raw_grab(w)
    base_t = time.time()
    pending = []          # [(t_target, tag, ev_idx)]
    n_ev = 0
    cursor_last = None
    cursor_still_since = time.time()
    hover_s = 0.0

    def emit(ev: dict):
        nonlocal n_ev
        n_ev += 1
        ev["idx"] = n_ev
        ev["t"] = round(time.time() - t0, 3)
        fj.write(json.dumps(ev, ensure_ascii=False) + "\n")
        fj.flush()

        tag = ev["kind"]
        extra = ev.get("detail", "")
        line = f"+{ev['t']:7.2f}s  {tag:<6} {extra}"
        if ev.get("client"):
            line += f"  client={tuple(ev['client'])}"
        if ev.get("hover") is not None:
            line += f"  hover={ev['hover']:.2f}s"
        if ev.get("fg"):
            line += f"  前台={ev['fg']}"
        shots = ev.get("shots") or []
        if shots:
            line += "  → " + ", ".join(shots)
        print(line, flush=True)
        fg.write(line + "\n")
        fg.flush()

    def event(kind: str, detail: str = "", with_shots: bool = False):
        nonlocal base, base_t
        cx, cy, _, _ = w.client_rect()
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        ev = {
            "kind": kind,
            "detail": detail,
            "screen": [pt.x, pt.y],
            "client": [pt.x - cx, pt.y - cy],
            "hover": round(hover_s, 2),
            "fg": fg_title(),
            "mods": mods(),
        }
        if with_shots and not args.no_shot:
            ev["shots"] = []
            for d, tag in ((0.08, "a"), (0.55, "b"), (1.30, "c")):
                pending.append((time.time() + d, tag, n_ev + 1))
            # 点击前的那一帧
            if base is not None and not args.no_shot:
                nm = f"e{n_ev + 1:03d}_pre.png"
                png_write(os.path.join(OUT, nm), base)
                ev["shots"].append(nm)
        emit(ev)
        return n_ev

    try:
        while True:
            now = time.time()
            if now - t0 > args.secs:
                print(f"\n到 {args.secs:.0f}s，停止录制。", flush=True)
                break
            if not w.alive():
                print("\nWar3 窗口没了，停止录制。", flush=True)
                break

            # —— 鼠标位置 / 悬停时长
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            if cursor_last != (pt.x, pt.y):
                cursor_last = (pt.x, pt.y)
                cursor_still_since = now
            hover_s = now - cursor_still_since

            # —— 边沿检测
            for vk in vks:
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                if down and not prev[vk]:
                    if vk in (0x01, 0x02, 0x04):
                        event("CLICK", VK_NAME[vk], with_shots=True)
                        base = None  # 强制刷新基线
                    elif args.keys and vk in VK_NAME:
                        event("KEY", VK_NAME[vk], with_shots=True)
                prev[vk] = down

            # —— 到点抓帧
            if pending:
                keep = []
                for t_target, tag, idx in pending:
                    if now >= t_target:
                        img = raw_grab(w)
                        if img is not None and not args.no_shot:
                            nm = f"e{idx:03d}_{tag}.png"
                            png_write(os.path.join(OUT, nm), img)
                            print(f"           ↳ 存帧 {nm}", flush=True)
                    else:
                        keep.append((t_target, tag, idx))
                pending = keep

            # —— 基线刷新 + 无操作突变检测
            if now - base_t >= args.baseline:
                img = raw_grab(w)
                if img is not None:
                    if base is not None and base.shape == img.shape:
                        d = float(np.abs(img.astype(np.int16) - base.astype(np.int16)).mean())
                        if d >= args.diff:
                            event("CHANGE", f"画面突变 diff={d:.1f}")
                            if not args.no_shot:
                                nm = f"e{n_ev:03d}_chg.png"
                                png_write(os.path.join(OUT, nm), img)
                                print(f"           ↳ 存帧 {nm}", flush=True)
                    base = img
                    base_t = now

            time.sleep(0.015)
    finally:
        fg.close()
        fj.close()
        print(f"\n共记录 {n_ev} 条事件 → {OUT}/trace.txt", flush=True)


if __name__ == "__main__":
    main()
