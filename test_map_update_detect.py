# -*- coding: utf-8 -*-
"""离线自检：把历史截图喂给 map_update 的判据，确认只有「地图已更新」命中。

用法： python test_map_update_detect.py
"""
import glob
import os
import sys

import cv2
import numpy as np

HERE = r"C:\Users\Administrator\Desktop\我在峡谷打\kk_auto"
sys.path.insert(0, HERE)

import map_update as mu  # noqa: E402

NEG = [
    r"C:\Users\Administrator\Desktop\我在峡谷打\kk_auto\logs\popup_unknown_102329.png",  # 断线重连
    r"C:\Users\Administrator\Desktop\我在峡谷打\kk_auto\logs\popup_unknown_100150.png",  # 正样本
    r"C:\Users\Administrator\Desktop\我在峡谷打\kk_auto\logs\popup_unknown_224657.png",
    r"C:\Users\Administrator\Desktop\我在峡谷打\kk_auto\logs\popup_unknown_231102.png",
]
POS = [
    r"C:\Users\Administrator\Desktop\我在峡谷打\kk_auto\logs\popup_unknown_100145.png",
    r"C:\Users\Administrator\Desktop\我在峡谷打\kk_auto\logs\popup_unknown_100146.png",
    r"C:\Users\Administrator\Desktop\我在峡谷打\kk_auto\logs\popup_unknown_100150.png",
    r"C:\Users\Administrator\AppData\Local\Temp\kk_probe\dlg_now_screen.png",
]

tpl = mu._load_tpl()
print("模板", tpl.shape if tpl is not None else None)


def score(path):
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    sc, scale, loc, _ = mu.bg._match(img, tpl)
    return sc


print("\n--- 应命中（>=0.80）---")
bad = 0
for p in POS:
    if not os.path.exists(p):
        print("  跳过（不存在）", os.path.basename(p))
        continue
    s = score(p)
    ok = s is not None and s >= mu.SCORE
    bad += 0 if ok else 1
    print(f"  {'✔' if ok else '✘'} {s:.3f}  {os.path.basename(p)}")

print("\n--- 不应命中（<0.80）---")
files = sorted(glob.glob(os.path.join(HERE, "logs", "popup_unknown_*.png")))
files += sorted(glob.glob(r"C:\Users\Administrator\AppData\Local\Temp\kk_probe\*client*.png"))
pos_names = {os.path.basename(p) for p in POS}
worst = 0.0
for p in files:
    if os.path.basename(p) in pos_names:
        continue
    s = score(p)
    if s is None:
        continue
    if s > worst:
        worst = s
    if s >= mu.SCORE:
        bad += 1
        print(f"  ✘ 误报 {s:.3f}  {os.path.basename(p)}")
print(f"  负样本最高分 {worst:.3f}（阈值 {mu.SCORE}）")

print("\n结论：", "全部通过" if bad == 0 else f"{bad} 项不通过")
sys.exit(0 if bad == 0 else 1)
