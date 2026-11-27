@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo ============================================
echo  正常退出 War3
echo  F10 游戏菜单 - E 结束游戏 - X 退出程序 - X 确认
echo  全程约 13-14 秒（每步间隔 3 秒），退出后平台房间仍在、可直接再开一局
echo  热键: F9 停止   F11 紧急释放
echo ============================================
"%PY%" -W ignore game_auto.py run --config configs\war3_exit.json --task quit_game --autostart
echo.
echo ============================================
tasklist /FI "IMAGENAME eq War3.exe" 2>nul | find /I "War3.exe" >nul
if errorlevel 1 (
  echo  War3 已退出
) else (
  echo  War3 还在运行 —— 可能是窗口没在前台，或状态异常
  echo  可以再跑一次，或手动 F10 - E - X - X
)
echo ============================================
pause
