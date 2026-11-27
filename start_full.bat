@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo ============================================
echo  全链路：搜地图 -^> 建房 -^> 填密码 -^> 开局 -^> 选最高难度开打
echo  前提：平台已开着（主窗口在前台），游戏没在跑
echo  热键: F9 停止   F10 紧急释放
echo ============================================
"%PY%" -W ignore game_auto.py pipeline --pipeline configs\pipeline.json
echo ============================================
echo  完成。若卡在大厅，可能是被「自动选择钓鱼模式」倒计时切走了
echo ============================================
pause
