@echo off
rem ============================================================
rem  Win7 dependency self-check (keep this file ASCII on purpose:
rem  the exe name is Chinese, so we find it by wildcard instead).
rem  1) runs the bundled env check through the packed exe
rem  2) opens core\logs\envcheck.json in notepad
rem  "bad": []  =  all dependencies present.
rem ============================================================
cd /d "%~dp0"
set "EXE="
for %%f in ("%~dp0*_v*.exe") do if exist "%%~ff" set "EXE=%%~ff"
if not defined EXE (
  echo [X] No app exe found next to this bat. Keep the whole folder together.
  pause
  exit /b 1
)
echo Using exe: %EXE%
echo Running self-check, please wait a few seconds...
"%EXE%" core\_envcheck.py
if not exist "%~dp0core\logs\envcheck.json" (
  echo [X] No report produced. The exe probably failed to start.
  echo     Run the auto-install EXE first, reboot if requested, then try again.
  pause
  exit /b 1
)
start "" notepad.exe "%~dp0core\logs\envcheck.json"
echo.
echo Report opened. Check the "bad" field:
echo   "bad": []           = OK, you can run the app now
echo   "bad": ["cv2", ...] = missing DLL, run the auto-install EXE and reboot
echo If it still fails, send core\logs\envcheck.json back.
echo.
pause
