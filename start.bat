@echo off
REM ============================================================
REM  LMG ERP v2 - one click start (thin wrapper -> platform.ps1)
REM  Backend : 8020   Frontend : 5180
REM  NOTE: this file must stay ASCII + CRLF line endings.
REM ============================================================
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\platform.ps1" -Action Start -Root "%ROOT%"
set "RC=%ERRORLEVEL%"

if "%RC%"=="0" (
  start "" "http://localhost:5180/"
)
endlocal & exit /b %RC%
