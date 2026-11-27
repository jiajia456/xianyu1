# -*- coding: utf-8 -*-
"""离线验证：秘境 NPC 光圈判据收紧后，不再把旁边的小青团认成光圈。

2026-11-18 21:31 实测数据：标签@(1120,203)，真光圈应在 (1102,272)，
但那一帧把 (1013,274) 的一小团青色（面积 631~751）当成了光圈，
点下去 4 次都点空、面板没开。本测试造一张同构的假图来复现。
"""
import sys, os, json, io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
import secret_challenge as sc

W, H = 1690, 921
LX, LY = 1120, 203          # 标签中心（实测）
JUNK = (1013, 274)          # 误认的那团青色（实测）
REAL = (1102, 272)          # 真光圈（11-16 实测）


def make_img(with_real: bool):
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = (60, 55, 50)                       # 地面底色
    # 小的青色椭圆环：外框 60x42、线宽 5 → 面积约 750，和日志里 631~751 同量级
    cv2.ellipse(img, JUNK, (30, 21), 0, 0, 360, (200, 180, 40), 5)   # 外框约 60x42
    if with_real:
        # 真光圈：60x42 实心椭圆 → 面积 ~1900（日志里另一台是 1600+）
        cv2.ellipse(img, REAL, (30, 21), 0, 0, 360, (210, 190, 45), -1)
    return img


def cfg_with(dx, area):
    d = sc.load_cfg()
    d = json.loads(json.dumps(d))
    d["npc_locate"]["circle_max_dx"] = dx
    d["npc_locate"]["circle_area"] = area
    return d


def near(pos, want, tol=2):
    """连通块质心会有 1~2px 取整误差，按容差比。"""
    if pos is None or want is None:
        return pos == want
    return abs(pos[0] - want[0]) <= tol and abs(pos[1] - want[1]) <= tol


def run_case(with_real, dx, area, want, name):
    img = make_img(with_real)
    lc = cfg_with(dx, area)["npc_locate"]
    got = sc._circle_below(img, LX, LY, lc, scale=1.0)
    pos = (got[0], got[1]) if got else None
    ok = near(pos, want)
    print(f"{'✅' if ok else '❌'} {name}: 期望 {want} 实际 {pos}（面积 {got[2] if got else '-'}）")
    return ok


def main():
    allok = True
    # ① 只有那团小青色（真光圈没认出来）→ 旧判据会误认
    allok &= run_case(False, 95, 600, JUNK, "① 旧判据(95/600) 误把旁边小青团当光圈")
    # ② 同一张图，新判据 → 认不出（返回 None，交给「按标签推算」）
    allok &= run_case(False, 55, 900, None, "② 新判据(55/900) 不再误认 → 退回按标签推算")
    # ③ 真光圈在（且更近）→ 新旧都该选中真光圈
    allok &= run_case(True, 95, 600, REAL, "③ 旧判据 真光圈在场时选真光圈")
    allok &= run_case(True, 55, 900, REAL, "④ 新判据 真光圈在场时选真光圈")

    # ⑤ 诊断图能落盘
    try:
        img = make_img(False)
        sc.LAST_LABEL = (LX, LY, "四字")
        cfg = cfg_with(55, 900)
        p = sc.dump_fail(img, JUNK, cfg)
        ok = os.path.exists(p) and os.path.getsize(p) > 1000
        print(f"{'✅' if ok else '❌'} ⑤ 诊断图落盘 {p} ({os.path.getsize(p) if ok else 0} B)")
        allok &= ok
    except Exception as e:
        print(f"❌ ⑤ 诊断图落盘异常：{e}")
        allok = False

    print("全部通过" if allok else "有失败")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
