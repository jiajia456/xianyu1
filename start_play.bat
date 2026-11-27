@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo ============================================
echo  一键开局
echo   1) 房间窗口：确认观战未勾选 → 点「开始游戏」
echo   2) 等 War3 起来进大厅 → 选最高可见难度（双击 x2）
echo   3) 点「开始游戏」x2
echo  热键: F9 停止   F10 紧急释放
echo ============================================
"%PY%" -W ignore game_auto.py run --config configs\wuxia_room.json --task start_game --autostart
"%PY%" -W ignore game_auto.py run --config configs\wuxia_lobby.json --task auto_start --autostart
pause
