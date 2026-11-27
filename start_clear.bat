@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo ============================================
echo   游戏通关（一键）
echo ------------------------------------------------------------
echo   ① 游戏中准备四步   技能 3 / 装备 4 / 升级武器 3 / 自动挑战
echo   ② 提前挑战         出现紫色水晶图标 -^> 双击 -^> 弹窗「是」
echo   ③ 挑战存档         出现「挑战存档」-^> 点击 -^> 通关
echo ------------------------------------------------------------
echo   监控频率 60 秒；F9 停止  F10 紧急释放
echo ============================================
echo.
echo ==== ① 游戏中准备四步 ====
"%PY%" -W ignore in_game.py
if errorlevel 1 (
  echo.
  echo 四步没跑通，先不进监控
  pause
  exit /b 1
)
echo.
echo ==== ②③ 提前挑战 / 挑战存档 常驻监控 ====
"%PY%" -W ignore watch_early.py --interval 60
pause
