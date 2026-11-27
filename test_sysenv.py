# -*- coding: utf-8 -*-
"""离线验证：系统识别与分档参数（2026-11-25 用户要求「开始运行就检测 W7/W10」）

覆盖：
  ① 版本号 → 档位映射（Win7 SP1 / Win10 / Win11 靠 build 区分 / 未知）
  ② 配置文件按档位覆盖 + 缺键回落默认 + 文件缺失不报错
  ③ _force_capture_mode 优先级最高；非法 capture_mode 被丢弃
  ④ describe_effect 只说真实生效的差异（默认档不应出现 ×1.0 这种噪音）
  ⑤ dump() 能落 logs/sysenv.json 且含 summary/settings

跑法：python test_sysenv.py
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sysenv as se

TMP = tempfile.mkdtemp(prefix="kksysenv_")


def main() -> int:
    allok = True

    # ---- ① 版本映射 ----
    try:
        cases = [
            ((6, 1, 7601), "win7", "Windows 7"),
            ((6, 2, 9200), "win8", "Windows 8"),
            ((6, 3, 9600), "win81", "Windows 8.1"),
            ((10, 0, 19045), "win10", "Windows 10"),
            ((10, 0, 22631), "win11", "Windows 11"),
            ((6, 0, 6002), "other", "Windows Vista"),
        ]
        bad = []
        for (mj, mn, bd), want_p, want_n in cases:
            p, n = se.classify(mj, mn, bd)
            if p != want_p or n != want_n:
                bad.append(((mj, mn, bd), (p, n)))
        ok = not bad
        print(f"{'✅' if ok else '❌'} ① 版本号映射（Win11 靠 build>=22000 区分）{bad or ''}")
        allok &= ok
    except Exception as e:
        print(f"❌ ① 映射异常：{e}")
        allok = False

    # ---- ② 配置覆盖 / 回落 ----
    try:
        cfg = os.path.join(TMP, "os_profile.json")
        with io.open(cfg, "w", encoding="utf-8") as f:
            json.dump({"default": {"hover_scale": 1.0, "capture_mode": "auto"},
                       "win7": {"hover_scale": 1.4},
                       "win10": {"capture_mode": "screen"}}, f)
        os.environ["KK_OS_PROFILE_CFG"] = cfg
        s7, s10, s_other = se.settings("win7"), se.settings("win10"), se.settings("win81")
        ok = (s7["hover_scale"] == 1.4 and s7["capture_mode"] == "auto"      # 覆盖 + 回落
              and s10["capture_mode"] == "screen" and s10["hover_scale"] == 1.0
              and s_other["capture_mode"] == "auto")                          # 未配置档 → 默认
        print(f"{'✅' if ok else '❌'} ② 按档覆盖 + 缺键回落（win7 hover={s7['hover_scale']}，"
              f"win10 capture={s10['capture_mode']}）")
        allok &= ok
    except Exception as e:
        print(f"❌ ② 配置异常：{e}")
        allok = False

    # ---- ②b 文件不存在时用内置默认 ----
    try:
        os.environ["KK_OS_PROFILE_CFG"] = os.path.join(TMP, "不存在.json")
        s = se.settings("win7")
        ok = s["hover_scale"] == 1.0 and s["capture_mode"] == "auto"   # v1.94：内置默认跟 Win10 一致
        print(f"{'✅' if ok else '❌'} ②b 配置缺失时回落内置默认（hover={s['hover_scale']}）")
        allok &= ok
    except Exception as e:
        print(f"❌ ②b 缺文件异常：{e}")
        allok = False

    # ---- ③ 强制抓图方式 / 非法值 ----
    try:
        cfg2 = os.path.join(TMP, "force.json")
        with io.open(cfg2, "w", encoding="utf-8") as f:
            json.dump({"win7": {"capture_mode": "printwindow"},
                       "_force_capture_mode": "screen"}, f)
        os.environ["KK_OS_PROFILE_CFG"] = cfg2
        ok = se.capture_mode("auto") == "screen"          # 强制优先
        os.environ["KK_OS_PROFILE_CFG"] = os.path.join(TMP, "bad.json")
        with io.open(os.environ["KK_OS_PROFILE_CFG"], "w", encoding="utf-8") as f:
            json.dump({"default": {"capture_mode": "乱写"}}, f)
        ok &= se.capture_mode("auto") == "auto"           # 非法值丢弃 → 回默认
        print(f"{'✅' if ok else '❌'} ③ 强制抓图方式优先、非法值回默认")
        allok &= ok
    except Exception as e:
        print(f"❌ ③ 抓图方式异常：{e}")
        allok = False

    # ---- ④ describe_effect 不出现无意义噪音 ----
    try:
        os.environ["KK_OS_PROFILE_CFG"] = os.path.join(TMP, "plain.json")
        with io.open(os.environ["KK_OS_PROFILE_CFG"], "w", encoding="utf-8") as f:
            json.dump({"default": {"hover_scale": 1.0, "focus_wait_scale": 1.0,
                                   "capture_mode": "auto"}}, f)
        plain = se.describe_effect()
        os.environ["KK_OS_PROFILE_CFG"] = os.path.join(TMP, "win7cfg.json")
        with io.open(os.environ["KK_OS_PROFILE_CFG"], "w", encoding="utf-8") as f:
            json.dump({"win7": {"hover_scale": 1.25, "focus_wait_scale": 1.2,
                                "capture_mode": "screen"}}, f)
        se._lock["info"] = dict(se.info(refresh=True))
        se._lock["info"]["profile"] = "win7"
        w7 = se.describe_effect()
        ok = ("×1.0" not in plain and "悬停时长 ×1.25" in w7 and "screen" in w7)
        print(f"{'✅' if ok else '❌'} ④ 日志描述：默认档「{plain}」，Win7 档「{w7}」")
        allok &= ok
    except Exception as e:
        print(f"❌ ④ 描述异常：{e}")
        allok = False

    # ---- ⑤ dump 落盘 ----
    try:
        se._lock["info"] = None
        p = se.dump()
        ok = bool(p) and os.path.exists(p)
        data = json.load(io.open(p, encoding="utf-8")) if ok else {}
        ok &= bool(data.get("summary")) and "settings" in data and "profile" in data
        print(f"{'✅' if ok else '❌'} ⑤ sysenv.json 落盘（{data.get('profile')} / "
              f"{data.get('summary')}）")
        allok &= ok
    except Exception as e:
        print(f"❌ ⑤ 落盘异常：{e}")
        allok = False

    os.environ.pop("KK_OS_PROFILE_CFG", None)
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    print("全部通过" if allok else "有失败")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
