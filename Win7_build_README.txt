v2.22 源码打包说明

这份源码包含当前的 Win7/Win10 KK 双窗口切换修复，修改文件为
game_auto.py 和 full_loop.py。

在 Windows 7 SP1 或 Windows 10 的 64 位电脑上构建：
1. 双击 一键构建_Win7_Win10.bat。若没有 Python 3.8 x64，
   脚本会从 python.org 下载官方安装程序，验证数字签名后安装到当前用户目录。
2. 首次构建需联网安装 Python 与依赖；下载或签名验证失败时，
   可从 https://www.python.org/downloads/release/python-3810/ 手动安装。
3. 按批处理窗口里的提示等待构建和自检完成。
4. 成功后取 dist\我在峡谷打僵尸_v2.22 文件夹或同名 ZIP。
   使用时必须保留整个文件夹，主 EXE 依赖同目录的 DLL 和 core 文件夹。

如果源码在 \\Mac\... 共享目录，批处理会尝试临时映射盘符。
若共享目录无法映射，先将整个「源码」文件夹复制到 Windows 本地磁盘，
例如 C:\KKBuild\源码，再运行批处理。

脚本设置 KK_NO_BUMP=1，避免把版本号自动改成 v2.23。
源码目录里附带的 我在峡谷打僵尸_全静默版.exe 是旧的辅助程序，
并非本次新编译的主程序；它不支持 Win7。

当前尚未在 Windows 真机执行这次打包或验证。
