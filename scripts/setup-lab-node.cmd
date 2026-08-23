@echo off
REM Windows wrapper: run the WSL installer. Does not install a boot task.
REM Usage:
REM   scripts\setup-lab-node.cmd --api http://10.10.126.226:8080
REM   scripts\setup-lab-node.cmd --api http://10.10.126.226:8080 --registry 10.10.126.226:5001
set DISTRO=Ubuntu
if not "%LAB_DISTRO%"=="" set DISTRO=%LAB_DISTRO%

where wsl >nul 2>&1
if errorlevel 1 (
  echo install WSL2 and Ubuntu first
  exit /b 1
)

for /f "delims=" %%i in ('wsl -d %DISTRO% wslpath -a "%~dp0setup-lab-node.sh"') do set WSLSCRIPT=%%i
if "%WSLSCRIPT%"=="" (
  echo could not map this script into WSL
  exit /b 1
)

wsl -d %DISTRO% -- bash "%WSLSCRIPT%" %*
exit /b %ERRORLEVEL%
