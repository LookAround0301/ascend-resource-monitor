@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-web.ps1" %*
if errorlevel 1 pause
