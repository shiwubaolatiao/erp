@echo off
REM ============================================================
REM  LMG ERP v2 - read-only status (thin wrapper -> platform.ps1)
REM  Never starts/stops processes, never takes the operation lock.
REM  NOTE: this file must stay ASCII + CRLF line endings.
REM ============================================================
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\platform.ps1" -Action Status -Root "%ROOT%"
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
