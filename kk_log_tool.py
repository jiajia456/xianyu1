# -*- coding: utf-8 -*-
"""KK 日志收集器 —— 双击运行（打包成「查看日志.exe」）

真正的实现在 logpack.py：GUI 底部的「打包日志发我」按钮走的也是它，改一处两边都生效。
本文件只负责命令行入口：收集 → 在资源管理器里选中 zip → 弹结果框。
"""
from __future__ import annotations

import os
import subprocess
import traceback

import logpack


def main() -> int:
    logpack.set_dpi_aware()
    info = {}
    try:
        info = logpack.pack()
        msg = f"已收集 {info['copied']} 个文件\n\n{info.get('zip') or info['out']}"
        ok = True
    except Exception:
        msg = "出错了：\n\n" + traceback.format_exc(limit=3)
        ok = False

    target = info.get("zip") or info.get("out") or logpack.desktop_dir()
    try:
        subprocess.Popen(f'explorer /select,"{target}"', creationflags=0x08000000)
    except Exception:
        try:
            os.startfile(os.path.dirname(target) or logpack.desktop_dir())
        except Exception:
            pass

    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        r.attributes("-topmost", True)
        if ok:
            messagebox.showinfo(logpack.APP + " · 完成",
                                msg + "\n\nzip 已选中，拖进聊天窗口即可发送。")
        else:
            messagebox.showerror(logpack.APP + " · 失败", msg)
        r.destroy()
    except Exception:
        print(msg)
        try:
            input("按回车关闭…")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
