@echo off
rem ============================================================================
rem Connect Atlas to Gmail, Google Calendar, Canvas and GitHub: double-click.
rem Opens each provider's token page, checks every token before saving it to
rem .env, then offers to restart Atlas so the worker starts reading.
rem Options: scripts\connect.bat --check   or   --redo canvas,github
rem ============================================================================
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Atlas is not installed yet. Run scripts\deploy.bat first.
  pause
  exit /b 2
)
.venv\Scripts\python.exe -m scripts.connect %*
if errorlevel 3 if not errorlevel 4 goto restart
echo.
pause
exit /b %errorlevel%

:restart
echo.
set /p RESTART=Restart Atlas now so it uses the new connections? (Y/n) 
if /i "%RESTART%"=="n" (
  echo Run scripts\deploy.bat -SkipPull later to restart.
  pause
  exit /b 0
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" -SkipPull
