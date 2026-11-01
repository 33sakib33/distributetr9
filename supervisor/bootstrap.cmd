@echo off
REM One-command lab PC setup. Run in an Administrator Command Prompt, e.g.
REM   supervisor\bootstrap.cmd -Api http://10.0.0.5:8080 -Registry 10.0.0.5:5001
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1" %*
exit /b %ERRORLEVEL%
