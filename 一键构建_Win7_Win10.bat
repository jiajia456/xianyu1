@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 (
    echo Cannot access the source folder. Copy the full folder to C: and retry.
    pause
    exit /b 1
)
set "KK_TARGET=win7"
set "KK_NO_BUMP=1"

set "KK_PY="
py -3.8 -c "import sys; assert sys.version_info[:2] == (3, 8) and sys.maxsize > 2**32" >nul 2>&1
if not errorlevel 1 set "KK_PY=py -3.8"
if defined KK_PY goto python_ready

for %%P in ("%LOCALAPPDATA%\KKAuto\Python38\python.exe" "%LOCALAPPDATA%\Programs\Python\Python38\python.exe" "C:\Python38\python.exe") do (
    if exist "%%~P" (
        "%%~P" -c "import sys; assert sys.version_info[:2] == (3, 8) and sys.maxsize > 2**32" >nul 2>&1
        if not errorlevel 1 (
            set "KK_PY=%%~P"
            goto python_ready
        )
    )
)

echo Python 3.8 x64 was not found. Downloading the official installer...
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\InstallPython38.ps1"
if errorlevel 1 goto python_install_failed
set "KK_PY=%LOCALAPPDATA%\KKAuto\Python38\python.exe"
if not exist "%KK_PY%" goto python_install_failed
"%KK_PY%" -c "import sys; assert sys.version_info[:2] == (3, 8) and sys.maxsize > 2**32" >nul 2>&1
if errorlevel 1 goto python_install_failed

:python_ready
echo Using Python: %KK_PY%
if exist "%KK_PY%" (
    "%KK_PY%" -m pip install -r "tools\requirements-win7.txt"
) else (
    %KK_PY% -m pip install -r "tools\requirements-win7.txt"
)
if errorlevel 1 goto dependencies_failed

if exist "%KK_PY%" (
    "%KK_PY%" "build_exe.py" --zip
) else (
    %KK_PY% "build_exe.py" --zip
)
if errorlevel 1 goto build_failed

echo Build succeeded. Check the dist folder for the complete ZIP.
pause
popd
exit /b 0

:python_install_failed
echo Automatic Python installation failed. Install Python 3.8.10 x64 manually.
echo https://www.python.org/downloads/release/python-3810/
pause
popd
exit /b 1

:dependencies_failed
echo Dependency installation failed. Check the network and retry.
pause
popd
exit /b 2

:build_failed
echo EXE build or validation failed. Do not distribute the output.
pause
popd
exit /b 3
