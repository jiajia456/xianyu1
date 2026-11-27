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
echo Checking platform windows without starting a game...
"%APP%" "core\quick_check.py"
set "CHECK_RC=%errorlevel%"
if exist "%~dp0core\logs\quick_check.json" (
  start "" notepad.exe "%~dp0core\logs\quick_check.json"
  echo Report: core\logs\quick_check.json
) else (
  echo No report was created. Run the auto-install EXE first.
)
echo This check does not wait for the 15-minute game screen.
pause
exit /b %CHECK_RC%
