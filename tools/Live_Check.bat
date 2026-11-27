@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "APP="
for %%F in ("%~dp0*_v*.exe") do if exist "%%~fF" set "APP=%%~fF"
if not defined APP (
  echo Main application was not found. Keep the whole folder together.
  pause
  exit /b 2
)
echo This test clicks Start Game in the existing room and waits up to 60 seconds for War3.
echo It leaves the game running and does not enter the 15-minute lobby wait.
"%APP%" "core\live_check.py"
set "CHECK_RC=%errorlevel%"
if exist "%~dp0core\logs\live_check.json" (
  start "" notepad.exe "%~dp0core\logs\live_check.json"
  echo Report: core\logs\live_check.json
) else (
  echo No report was created. Run the auto-install EXE first.
)
pause
exit /b %CHECK_RC%
