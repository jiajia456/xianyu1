@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo ============================================
echo  搜索地图：我在峡谷打僵尸 → 双击封面
echo  热键: F8 开始   F9 停止   F10 紧急释放
echo  先把平台窗口切到前台，再按 F8
echo ============================================
"%PY%" -W ignore game_auto.py run --config configs\wuxia.json --task search_double
pause
