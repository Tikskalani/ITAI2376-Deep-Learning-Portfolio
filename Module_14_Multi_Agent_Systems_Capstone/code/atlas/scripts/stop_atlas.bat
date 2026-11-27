@echo off
rem ============================================================================
rem Stop Atlas (double-click; one UAC click).
rem Stops the AtlasWorker and AtlasWeb services and waits until both are
rem down. Postgres and Ollama stay up: they are idle when Atlas is off, and
rem the Atlas MCP server in Claude sessions reads from Postgres. To stop them
rem as well:  scripts\stop_atlas.bat all
rem Without the services installed, it ends the dashboard window the old way.
rem Testing from a non-admin window: ATLAS_SKIP_ELEVATION=1 skips the UAC step,
rem ATLAS_DOCKER_CMD replaces the docker command (for example "call stub.bat").
rem ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0.."
set "DOCKER=docker"
if defined ATLAS_DOCKER_CMD set "DOCKER=%ATLAS_DOCKER_CMD%"

sc query AtlasWeb >nul 2>&1
if errorlevel 1 goto :noservice

if defined ATLAS_SKIP_ELEVATION goto :elevated
net session >nul 2>&1
if errorlevel 1 (
  echo [atlas] the services need an admin window; asking Windows for one...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
  exit /b 0
)

:elevated
title Atlas: stopping
echo.
set "FAILED="
call :stop_service AtlasWorker
if errorlevel 1 set "FAILED=1"
call :stop_service AtlasWeb
if errorlevel 1 set "FAILED=1"
if /i "%~1"=="all" (
  echo.
  echo [atlas] stopping Postgres and Ollama too ^(data stays^)...
  %DOCKER% compose stop
)
echo.
if defined FAILED (
  echo [atlas] Atlas is not fully off; see the lines above.
) else (
  echo [atlas] Atlas is off. Double-click Start Atlas to bring it back.
)
goto :done

rem ---------------------------------------------------------------- subroutines

:sleep
rem ping needs no console, unlike timeout.exe, which exits when stdin is redirected.
set /a pings=%1+1
ping -n !pings! 127.0.0.1 >nul
exit /b 0

:stop_service
set "STATE="
for /f "tokens=4" %%t in ('sc query %1 ^| findstr STATE') do set "STATE=%%t"
if "!STATE!"=="STOPPED" (
  echo [atlas] %1 already stopped.
  exit /b 0
)
echo [atlas] stopping %1 ^(was !STATE!^)...
sc stop %1 2>&1 | findstr /i "denied" >nul && (
  echo         Windows refused: this window is not elevated. Double-click Stop Atlas on the Desktop instead.
  exit /b 1
)
set /a tries=0
:stop_service_wait
for /f "tokens=4" %%t in ('sc query %1 ^| findstr STATE') do set "STATE=%%t"
if "!STATE!"=="STOPPED" (
  echo         %1 stopped.
  exit /b 0
)
set /a tries+=1
if !tries! geq 20 (
  echo         %1 is still !STATE! after forty seconds. scripts\deploy.bat reinstalls both services cleanly.
  exit /b 1
)
call :sleep 2
goto :stop_service_wait

rem ---------------------------------------------------------------- no service

:noservice
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do (
  echo [atlas] stopping dashboard pid %%p
  taskkill /pid %%p /f >nul 2>&1
)
echo [atlas] dashboard stopped.

:done
echo.
pause
endlocal
