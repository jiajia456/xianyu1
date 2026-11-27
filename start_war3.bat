@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo === 魔兽RPG 技能循环挂机 ===
echo 热键: F8 开始或暂停   F9 停止   F10 紧急释放
"%PY%" -W ignore game_auto.py run --config configs\war3.json --task skill_loop
pause
