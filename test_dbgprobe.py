# -*- coding: utf-8 -*-
"""离线验证：调试探针（2026-11-25 用户要求「成品新增调试按钮开关」）

覆盖五件事：
  ① 开关只看 KK_DEBUG，关着时所有写盘动作都不发生（不能拖累正常运行）
  ② 事件流 events.jsonl 是合法 JSON Lines，中文不转义
  ③ 截图会画十字标记、走 imencode+tofile（中文路径也能存）、有节流
  ④ 模板打分能在一张已知布局的假图上认出贴上去的模板
  ⑤ snapshot() 在「没有游戏窗口」的情况下也能产出完整证据包（不依赖真桌面）

跑法：python test_dbgprobe.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import cv2

import dbgprobe as dp

TMP = tempfile.mkdtemp(prefix="kkdbg_")


def fake_img(w=800, h=600):
    rng = np.random.RandomState(7)
    img = (rng.rand(h, w, 3) * 60 + 40).astype("uint8")
    # 贴一块有纹理的「模板」到固定位置（用同一张图当模板 → 分数必然很高）
    tpl = (rng.rand(28, 72, 3) * 200 + 30).astype("uint8")
    img[200:228, 300:372] = tpl
    return img, tpl


def main() -> int:
    allok = True
    os.environ["KK_DEBUG_DIR"] = TMP
    os.environ.pop("KK_DEBUG", None)

    # ---- ① 关着的时候什么都不写 ----
    try:
        ok = (not dp.on())
        p = dp.shot(np.zeros((50, 50, 3), "uint8"), "off")
        dp.note("off", x=1)
        ok &= (p == "")
        ok &= (not os.path.exists(os.path.join(TMP, "events.jsonl")))
        print(f"{'✅' if ok else '❌'} ① 开关关闭时不产生任何文件")
        allok &= ok
    except Exception as e:
        print(f"❌ ① 关闭态异常：{e}")
        allok = False

    # ---- ② 事件流 ----
    try:
        os.environ["KK_DEBUG"] = "1"
        dp.note("unit", 模板="测试", 分数=0.987)
        path = os.path.join(dp.session_dir(), "events.jsonl")
        lines = [json.loads(x) for x in io.open(path, encoding="utf-8") if x.strip()]
        ok = (len(lines) == 1 and lines[0]["tag"] == "unit"
              and lines[0]["模板"] == "测试" and lines[0]["pid"] > 0)
        raw = io.open(path, encoding="utf-8").read()
        ok &= ("测试" in raw)          # 中文没被转成 \uXXXX
        print(f"{'✅' if ok else '❌'} ② events.jsonl 合法且中文不转义")
        allok &= ok
    except Exception as e:
        print(f"❌ ② 事件流异常：{e}")
        allok = False

    # ---- ③ 截图 + 十字标记 + 节流 ----
    try:
        img, _ = fake_img()
        dp.SHOT_MIN_GAP = 0.0
        p1 = dp.shot(img, "click_测试模板", marks=[(320, 210, "tpl(0.99)→(320,210)")],
                     title="click x2")
        ok = bool(p1) and os.path.exists(p1)
        saved = cv2.imdecode(np.fromfile(p1, np.uint8), cv2.IMREAD_COLOR) if ok else None
        ok &= saved is not None and saved.shape == img.shape
        # 标记画上去了：点周围应出现接近纯红的像素（BGR 的 (0,0,255)）
        red = int(((saved[200:225, 310:335, 2] > 200) & (saved[200:225, 310:335, 1] < 80)).sum())
        ok &= red > 10
        # 中文文件名能落盘（走 imencode+tofile）
        ok &= ("click_测试模板" in os.path.basename(p1))
        print(f"{'✅' if ok else '❌'} ③ 截图带十字标记、中文名落盘（红像素 {red} 个）")
        allok &= ok
    except Exception as e:
        print(f"❌ ③ 截图异常：{e}")
        allok = False

    # ---- ③b 张数上限：满了挤掉最旧的（保留最新 N 张）----
    try:
        dp.MAX_SHOTS = 3
        dp._S["shot_paths"] = []
        dp._S["shots"] = 0
        got = [dp.shot(img, f"cap{i}") for i in range(6)]
        left = sorted(n for n in os.listdir(dp.session_dir()) if "_cap" in n)
        ok = len(got) == 6 and len(left) == 3
        ok &= ("cap5" in left[-1]) if left else False      # 最新的那张一定在
        print(f"{'✅' if ok else '❌'} ③b 截图额度满后保留最新（{len(left)}/6 张，"
              f"最新={left[-1] if left else '-'}）")
        allok &= ok
    except Exception as e:
        print(f"❌ ③b 上限异常：{e}")
        allok = False

    # ---- ④ 模板打分 ----
    try:
        img, tpl = fake_img()
        tdir = os.path.join(TMP, "tpls")
        os.makedirs(tdir, exist_ok=True)
        ok, buf = cv2.imencode(".png", tpl)
        buf.tofile(os.path.join(tdir, "已知模板.png"))
        # 再来一张纯噪声当「不存在的模板」，分数应该明显低
        noise = np.random.RandomState(99).randint(0, 255, (28, 72, 3)).astype("uint8")
        ok2, buf2 = cv2.imencode(".png", noise)
        buf2.tofile(os.path.join(tdir, "无关模板.png"))
        dp.MAX_SHOTS = 100
        rows = dp.score_templates(img, tdir)
        by = {r["tpl"]: r["score_half"] for r in rows}
        ok = (len(rows) == 2 and by.get("已知模板.png", 0) > 0.9
              and by.get("无关模板.png", 1) < 0.8)
        print(f"{'✅' if ok else '❌'} ④ 模板打分：已知 {by.get('已知模板.png')} / "
              f"无关 {by.get('无关模板.png')}")
        allok &= ok
    except Exception as e:
        print(f"❌ ④ 打分异常：{e}")
        allok = False

    # ---- ⑤ snapshot 不依赖真桌面 ----
    try:
        img, _ = fake_img(1024, 768)
        dp._grab_screen = lambda: img
        dp._grab_rect = lambda x, y, w, h: img
        dp.windows_info = lambda: [
            {"title": "Warcraft III", "cls": "Warcraft III", "pid": 1234,
             "rect": [0, 0, 1024, 768]},
            {"title": "提示", "cls": "Dialog", "pid": 1234, "rect": [0, 0, 100, 80]},
        ]
        res = dp.snapshot(tag="单测")
        names = {os.path.basename(f) for f in res["files"]}
        ok = ("环境.json" in names and "窗口.json" in names
              and any(n.startswith("屏幕") for n in names))
        ok &= os.path.exists(os.path.join(res["dir"], "模板打分.json"))
        ok &= len(res["summary"]) >= 2
        print(f"{'✅' if ok else '❌'} ⑤ snapshot 产出证据包（{len(res['files'])} 个文件）")
        allok &= ok
    except Exception as e:
        print(f"❌ ⑤ snapshot 异常：{e}")
        allok = False

    try:
        shutil.rmtree(TMP, ignore_errors=True)
    except Exception:
        pass
    print("全部通过" if allok else "有失败")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
