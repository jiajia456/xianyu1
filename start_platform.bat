@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo === 平台自动进房（需要先截好模板，见 README） ===
"%PY%" -W ignore game_auto.py run --config configs\platform.json --task enter_game
pause
