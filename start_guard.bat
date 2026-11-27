@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo ============================================
echo  随机弹窗看门狗
echo  每 3 秒扫一次，出现「我要变强」等弹窗就点
echo  「我知道了」自动关掉
echo  热键: F9 停止   F10 紧急释放
echo ============================================
"%PY%" -W ignore game_auto.py run --config configs\wuxia.json --task popup_guard --autostart
pause
