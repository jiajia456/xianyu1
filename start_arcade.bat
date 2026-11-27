@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo === 街机模块 自动投币 / 连打 ===
echo 热键: F8 开始或暂停   F9 停止   F10 紧急释放
echo 请先让 KK官方对战平台 进入游戏画面，再按 F8
"%PY%" -W ignore game_auto.py run --config configs\arcade.json --task main
pause
