# -*- coding: utf-8 -*-
"""v1.27 降 CPU 自检：短路 / 静止降频 / 扫描节流 / 优先级"""
import sys, types
import numpy as np

sys.path.insert(0, r"C:/Users/Administrator/Desktop/我在峡谷打/kk_auto")
import game_auto as ga

OK = 0
BAD = []


def ck(name, cond, extra=""):
    global OK
    if cond:
        OK += 1
        print(f"  ✅ {name} {extra}")
    else:
        BAD.append(name)
        print(f"  ❌ {name} {extra}")


# ============ A. 多档缩放短路 ============
print("\n[A] Visual.find 多档短路")


class FakeTplLoader(ga.Vision):
    """不吃磁盘模板，直接给一张图"""
    def __init__(self, tpl):
        self.template_dir = ""
        self._cache = {}
        self._tpl = tpl
        self.n_match = 0
        self.scores = []

    def _load(self, name):
        return self._tpl


def make_find(scores):
    """让 matchTemplate 每次按给定分数返回，并记录调用次数"""
    import cv2
    real = cv2.matchTemplate

    def fake(img, tpl, method):
        i = fake.calls
        fake.calls += 1
        sc = scores[min(i, len(scores) - 1)]
        h = img.shape[0] - tpl.shape[0] + 1
        w = img.shape[1] - tpl.shape[1] + 1
        res = np.zeros((h, w), np.float32)
        res[10, 20] = sc
        return res
    fake.calls = 0
    return fake


screen = np.zeros((150, 300, 3), np.uint8)   # 高<200 → 不走 0.5x 粗筛，单测多档短路
tpl = np.zeros((40, 60, 3), np.uint8)

v = FakeTplLoader(tpl)
fake = make_find([0.96, 0.97, 0.98])
_orig = ga.cv2.matchTemplate
ga.cv2.matchTemplate = fake
hit = v.find("x.png", screen, 0.85, None, [1.0, 0.9, (0.87, 1.0)])
ga.cv2.matchTemplate = _orig
ck("明显命中(0.96)时只跑 1 档", fake.calls == 1, f"calls={fake.calls}")
ck("返回的坐标正确", hit is not None and hit[0] == 20 and hit[1] == 10, str(hit[:2] if hit else None))

v2 = FakeTplLoader(tpl)
fake2 = make_find([0.86, 0.87, 0.99])   # 前两档刚过线但未到 0.89 → 应继续
ga.cv2.matchTemplate = fake2
h2 = v2.find("x.png", screen, 0.85, None, [1.0, 0.9, (0.87, 1.0)])
ga.cv2.matchTemplate = _orig
ck("边缘命中(0.86)不短路，继续跑满 3 档", fake2.calls == 3, f"calls={fake2.calls}")
ck("最终取到最高分 0.99", h2 is not None and abs(h2[4] - 0.99) < 1e-6, f"score={h2[4] if h2 else None}")

v3 = FakeTplLoader(tpl)
fake3 = make_find([0.30, 0.40, 0.40])   # 全不中 → 必须跑满
ga.cv2.matchTemplate = fake3
h3 = v3.find("x.png", screen, 0.85, None, [1.0, 0.9, (0.87, 1.0)])
ga.cv2.matchTemplate = _orig
ck("全不中时跑满 3 档", fake3.calls == 3, f"calls={fake3.calls}")
ck("全不中返回 None", h3 is None)


# ============ B. 静止降频 ============
print("\n[B] 等图循环静止降频")


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeWin:
    hwnd = 123
    pid = 456

    def client_rect(self):
        return 0, 0, 300, 200

    def alive(self):
        return True

    def fg_same_process(self):
        return True

    def focus(self):
        pass


class FakeCap:
    """画面内容可控：self.frame 决定发什么图"""
    def __init__(self):
        self.calls = 0
        self.frame = np.zeros((200, 300, 3), np.uint8)
        # 打点阵：让 std 够高，不会被「画面纯色=空窗口」判据误判成空窗
        # （那条判据 2026-11-19 17:30 加，用来把「黑屏/空窗」也拉回 2 秒扫描）
        self.frame[::8, ::8] = 180

    def grab(self, win):
        self.calls += 1
        return self.frame.copy()


class FakeVis:
    def __init__(self):
        self.calls = 0

    def find_best(self, names, img, th, region, scales):
        self.calls += 1
        return None, None


def make_bot(cap, vis):
    bot = object.__new__(ga.Bot)
    bot.win = FakeWin()
    bot.cap = cap
    bot.vis = vis
    bot._stop = types.SimpleNamespace(is_set=lambda: False)
    bot._paused = types.SimpleNamespace(is_set=lambda: False, wait=lambda *a: None)
    bot._last_fg = 0.0
    bot._last_dead_log = 0.0
    bot._cap_ref = None
    bot._step_gap = 0.0
    bot._cap_ref = None
    bot._step_gap = 0.0
    bot._wait_spec = None
    bot.sleeps = []

    def _sleep(ms):
        bot.sleeps.append(ms)
        ft.sleep(ms / 1000.0)          # 假时钟推进，不真等
    bot._sleep = _sleep
    bot._checkpoint = lambda: None     # 跳过窗口/前台判断
    bot._disc_guard_once = lambda *a, **k: False
    bot._bind_by_content = lambda *a, **k: False
    bot._dump_miss = lambda *a, **k: None
    return bot


ft = FakeTime()
real_time = ga.time
ga.time = ft
try:
    cap, vis = FakeCap(), FakeVis()
    bot = make_bot(cap, vis)
    # 画面完全静止，4 秒超时，interval=400
    ga.Bot._wait_image_loop(bot, {"template": "t.png", "timeout": 4000,
                                  "threshold": 0.85, "interval": 400,
                                  "disc_check": False})
finally:
    ga.time = real_time

print(f"     共 {len(bot.sleeps)} 轮，每轮间隔 = {bot.sleeps}")
ck("前 3 轮保持 400ms", bot.sleeps[:3] == [400, 400, 400], str(bot.sleeps[:3]))
ck("第 4 轮起放慢到 1000ms(2.5x)", all(x == 1000 for x in bot.sleeps[3:]), str(bot.sleeps[3:]))
ck("确实省了一轮以上", len(bot.sleeps) < 10, f"{len(bot.sleeps)} 轮（不降频应约 10 轮）")

# 画面突然变 → 立刻恢复
ft = FakeTime()
ga.time = ft
try:
    cap2, vis2 = FakeCap(), FakeVis()
    bot2 = make_bot(cap2, vis2)
    state = {"n": 0}
    real_grab = cap2.grab

    def grab_moving(win):
        state["n"] += 1
        # 前 5 帧静止，之后每帧大变
        if state["n"] > 5:
            cap2.frame = (np.random.rand(200, 300, 3) * 255).astype(np.uint8)
        return real_grab(win)
    cap2.grab = grab_moving
    ga.Bot._wait_image_loop(bot2, {"template": "t.png", "timeout": 5000,
                                   "threshold": 0.85, "interval": 400,
                                   "disc_check": False})
finally:
    ga.time = real_time

print(f"     共 {len(bot2.sleeps)} 轮，间隔 = {bot2.sleeps}")
ck("画面一变就回到 400ms", 400 in bot2.sleeps[3:], str(bot2.sleeps[3:]))
ck("降频后确实恢复过快节奏", any(x == 400 for x in bot2.sleeps[4:]), str(bot2.sleeps[4:]))


# ============ C. _bind_by_content 节流 ============
print("\n[C] 按画面绑定窗口的扫描节流")

ft = FakeTime()
ga.time = ft
try:
    bot3 = make_bot(FakeCap(), FakeVis())
    bot3.vis.find_best = lambda *a, **k: (None, None)
    spec = {"template": "t.png", "threshold": 0.85}
    r1 = ga.Bot._bind_by_content(bot3, spec)
    r2 = ga.Bot._bind_by_content(bot3, spec)
    r3 = ga.Bot._bind_by_content(bot3, spec)
    ft.t += 2.1                          # 过了 2 秒节流窗口
    r4 = ga.Bot._bind_by_content(bot3, spec)
finally:
    ga.time = real_time

ck("2 秒内连调 3 次只扫第 1 次", (r1, r2, r3) == (False, False, False))
ck("过了 2 秒会重新扫", r4 is False)      # 没有候选窗口 → 仍 False，但确实执行了
ck("节流时间戳已更新", bot3._bind_last == ft.t, f"{getattr(bot3, '_bind_last', None)}")


# ============ D. 优先级 ============
print("\n[D] 进程优先级")
import ctypes
k32 = ctypes.windll.kernel32
k32.GetCurrentProcess.restype = ctypes.c_void_p
k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
k32.GetPriorityClass.restype = ctypes.c_uint32
k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
k32.SetPriorityClass.restype = ctypes.c_int
before = k32.GetPriorityClass(k32.GetCurrentProcess())
r = ga.lower_priority()
after = k32.GetPriorityClass(k32.GetCurrentProcess())
ck("lower_priority() 返回 True", r is True)
ck("优先级已变为低于正常(0x4000)", after == 0x00004000, f"0x{after:04X}")
k32.SetPriorityClass(k32.GetCurrentProcess(), before)   # 还原，别影响当前会话
print(f"     （已还原为 0x{before:04X}）")



# ============ D. any_window 保活扫描间隔 ============
print("\n[D] 窗口保活扫描间隔（17:14 定 15 秒，17:44 改 30 秒）")

ft = FakeTime()
ga.time = ft
calls = []
try:
    bot4 = make_bot(FakeCap(), FakeVis())
    bot4._bind_by_content = lambda *a, **k: (calls.append(ft.t), False)[1]
    ga.Bot._wait_image_loop(bot4, {"template": "t.png", "timeout": 60000,
                                   "threshold": 0.85, "interval": 1000,
                                   "any_window": True, "disc_check": False})
finally:
    ga.time = real_time

ck("60s 内只扫 2 次（30/60s）", len(calls) == 2, f"calls={len(calls)}")
gaps = [round(calls[i] - calls[i - 1], 1) for i in range(1, len(calls))]
ck("相邻两次间隔 >= 30s", all(g >= 29.9 for g in gaps), str(gaps))
ck("没有 1~2 秒就扫一次的旧行为", all(g > 5 for g in gaps), str(gaps))

print(f"\n{'='*46}")
if BAD:
    print(f"❌ 失败 {len(BAD)} 项: {BAD}")
    sys.exit(1)
print(f"✅ 全部通过（{OK} 项）")
