@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update.ps1" %*
exit /b %ERRORLEVEL%
