@echo off
REM ============================================================
REM  LMG ERP v2 - restart (thin wrapper -> platform.ps1)
REM  Equivalent to a safe Stop followed by a full Start, under one lock.
REM  NOTE: this file must stay ASCII + CRLF line endings.
REM ============================================================
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\platform.ps1" -Action Restart -Root "%ROOT%"
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%