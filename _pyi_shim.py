# -*- coding: utf-8 -*-
"""PyInstaller 启动垫片：宿主机对刚生成的 exe 偶尔返回 EACCES，
PyInstaller 的重试只在白名单错误码上生效，这里把 chmod 失败降级成警告。"""
import os
import sys

_orig = os.chmod


def _chmod(path, mode, *a, **k):
    try:
        return _orig(path, mode, *a, **k)
    except OSError as e:
        print("[shim] chmod 跳过（不影响运行）%s: %s" % (path, e), flush=True)
        return None


os.chmod = _chmod

from PyInstaller.__main__ import run  # noqa: E402

sys.argv = ["pyinstaller"] + sys.argv[1:]
run()
