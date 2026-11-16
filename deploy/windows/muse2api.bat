@echo off
rem ============================================================
rem  muse2api Windows launcher
rem  Thin ASCII wrapper: delegates all logic to muse2api.ps1.
rem  Usage: double-click for menu, or: muse2api.bat start|stop|restart|status
rem ============================================================
setlocal
set "PS1=%~dp0muse2api.ps1"
if not exist "%PS1%" (
  echo [X] muse2api.ps1 not found next to this file.
  pause
  exit /b 1
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
