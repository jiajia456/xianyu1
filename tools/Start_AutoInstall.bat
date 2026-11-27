@echo off
setlocal EnableExtensions
set "BASE=%~dp0"
set "APP="
for %%F in ("%BASE%*_v*.exe") do if exist "%%~fF" set "APP=%%~fF"
if not defined APP (
  echo Main application was not found in this folder.
  pause
  exit /b 2
)

rem The x64 VC++ 2015 runtime is shared by Windows 7 SP1 and Windows 10.
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed 2>nul | findstr /i /r /c:"Installed *REG_DWORD *0x1" >nul
if not errorlevel 1 goto run_app

set "REDIST=%TEMP%\KKAuto_vc_redist_2015_x64.exe"
if not exist "%REDIST%" (
  echo Downloading the Win7-compatible Microsoft VC++ 2015 x64 runtime...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; (New-Object Net.WebClient).DownloadFile('https://download.microsoft.com/download/6/A/A/6AA4EDFF-645B-48C5-81CC-ED5963AEAD48/vc_redist.x64.exe',(Join-Path $env:TEMP 'KKAuto_vc_redist_2015_x64.exe'))"
  if errorlevel 1 (
    del /q "%REDIST%" 2>nul
    bitsadmin /transfer KKAutoVC /download /priority normal "https://download.microsoft.com/download/6/A/A/6AA4EDFF-645B-48C5-81CC-ED5963AEAD48/vc_redist.x64.exe" "%REDIST%"
  )
)
if not exist "%REDIST%" goto download_failed

rem Check Microsoft's code signature before running a downloaded installer.
set "KK_VC_FILE=%REDIST%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$s=Get-AuthenticodeSignature -FilePath $env:KK_VC_FILE; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike '*Microsoft Corporation*') { exit 1 }"
if errorlevel 1 (
  echo The VC++ installer signature could not be verified. Installation stopped.
  pause
  exit /b 3
)

echo Installing the Microsoft VC++ runtime. Approve the Windows UAC prompt.
start /wait "" "%REDIST%" /install /passive /norestart
set "INSTALL_RC=%errorlevel%"
if "%INSTALL_RC%"=="3010" (
  echo Installation completed. Restart Windows, then run this launcher again.
  pause
  exit /b 3010
)
if "%INSTALL_RC%"=="1638" goto run_app
if not "%INSTALL_RC%"=="0" (
  echo VC++ installation failed with exit code %INSTALL_RC%.
  pause
  exit /b %INSTALL_RC%
)

:run_app
cd /d "%BASE%"
start "" "%APP%"
rem Open KK automatically after the automation window appears.
start "" "%APP%" "core\plat_restart.py" --ensure
exit /b 0

:download_failed
echo Automatic download failed. Check the network and run this launcher again.
echo Microsoft download page: https://www.microsoft.com/download/details.aspx?id=53840
pause
exit /b 4
