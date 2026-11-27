@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe
if not exist "%PY%" set PY=python
echo ============================================================
echo   通关循环（一局）
echo   A 进场 -^> B 局内准备 -^> C 提前挑战+挑战存档
echo   -^> D 存档挑战(12格) -^> E 秘境挑战 -^> F 胜利收尾
echo ------------------------------------------------------------
echo   阶段开关在 configs\full_loop.json
echo   要打哪个秘境改 island / undead（true=打）
echo   命令行可加: --rounds 3  --island  --no-secret  --dry
echo ============================================================
echo.
"%PY%" -W ignore full_loop.py %*
echo.
pause
