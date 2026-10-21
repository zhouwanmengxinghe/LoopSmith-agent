@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "LOOPSMITH_EXIT_CODE=%ERRORLEVEL%"
if not "%LOOPSMITH_EXIT_CODE%"=="0" pause
exit /b %LOOPSMITH_EXIT_CODE%
