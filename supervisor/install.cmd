@echo off
REM Admin Command Prompt or PowerShell: bypasses Restricted execution policy.
REM Do not pass a leading slash in AGENTDIR. PowerShell treats /home as a switch.
set DISTRO=%~1
if "%DISTRO%"=="" set DISTRO=Ubuntu
set API=%~2
if "%API%"=="" set API=http://192.168.68.118:8080
set AGENTDIR=%~3
if "%AGENTDIR%"=="" set AGENTDIR=home/sakib/distributetr9_node
set WSLUSER=%~4
if "%WSLUSER%"=="" set WSLUSER=sakib

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Distro "%DISTRO%" -Api "%API%" -AgentDir "%AGENTDIR%" -WslUser "%WSLUSER%"
if errorlevel 1 exit /b 1
