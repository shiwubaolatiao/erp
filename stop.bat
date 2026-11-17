@echo off
REM ============================================================
REM  LMG ERP v2 - safe stop (thin wrapper -> platform.ps1)
REM  Stops only processes verified to belong to this project.
REM  NOTE: this file must stay ASCII + CRLF line endings.
REM ============================================================
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\platform.ps1" -Action Stop -Root "%ROOT%"
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
