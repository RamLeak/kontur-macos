@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0assemble-offline-download.ps1" -DownloadMissing %*
exit /b %ERRORLEVEL%
