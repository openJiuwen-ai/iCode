@echo off
rem [guard] Run guardctl.py (the guard switch manager) without uv.
rem
rem The sibling _launcher.ps1 finds a usable interpreter at run time: it scans
rem PATH for a real python (skipping the Microsoft Store stub that only opens
rem the store) and falls back to the interpreter the packaged iCode unpacks
rem under LOCALAPPDATA\pyapp.  So this works on machines that have no uv and
rem no python on PATH.
rem
rem Usage:  guardctl.cmd status | list | show IN-07 | disable IN-07 | on | off
rem         guardctl.cmd workspace restrict | workspace allow | reset
rem
rem Without arguments it prints the status and waits for a key (double-click
rem friendly).  Keep this file ASCII-only: Windows PowerShell 5.1 parses
rem BOM-less files as ANSI.
setlocal
if not exist "%~dp0_launcher.ps1" (
  echo [guard] _launcher.ps1 is missing next to this script.
  exit /b 1
)
if not exist "%~dp0guardctl.py" (
  echo [guard] guardctl.py is missing next to this script.
  exit /b 1
)
set "GUARD_SCRIPT=%~dp0guardctl.py"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0_launcher.ps1" %*
set "GUARD_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %GUARD_EXIT%
