@echo off
rem ============================================================================
rem Start Atlas (double-click; one UAC click).
rem   1. Docker Desktop up (started if it is not), Postgres and Ollama up,
rem      Postgres healthy.
rem   2. The AtlasWeb and AtlasWorker services started, or resumed when NSSM
rem      paused them (it does that after a reboot, when the app came up before
rem      Postgres did and crashed a few times in a row).
rem   3. Wait for /api/health, then open the dashboard.
rem Without the services installed (no deploy yet), it starts the dashboard
rem window the old way. Status at any time: scripts\status.bat
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
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)

:elevated
title Atlas: starting
echo.
echo [atlas] 1/3  Docker, Postgres and Ollama
call :ensure_docker
if errorlevel 1 goto :done
%DOCKER% compose -f docker-compose.yml -f docker-compose.dev.yml up -d
if errorlevel 1 (
  echo         docker compose failed; read the lines above.
  goto :done
)
call :wait_postgres
if errorlevel 1 goto :done

echo.
echo [atlas] 2/3  the services
call :start_service AtlasWeb
call :start_service AtlasWorker

echo.
echo [atlas] 3/3  waiting for the dashboard
call :wait_health
if errorlevel 1 goto :done
echo         Atlas is up: http://localhost:8000
start "" http://localhost:8000
goto :done

rem ---------------------------------------------------------------- subroutines

:sleep
rem ping needs no console, unlike timeout.exe, which exits when stdin is redirected.
set /a pings=%1+1
ping -n !pings! 127.0.0.1 >nul
exit /b 0

:ensure_docker
%DOCKER% info >nul 2>&1
if not errorlevel 1 exit /b 0
echo         Docker Desktop is not running; starting it ^(this can take a minute^)...
start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
set /a tries=0
:ensure_docker_wait
call :sleep 5
%DOCKER% info >nul 2>&1
if not errorlevel 1 exit /b 0
set /a tries+=1
if !tries! lss 36 goto :ensure_docker_wait
echo         Docker Desktop did not come up in three minutes. Open it yourself, then run this again.
exit /b 1

:wait_postgres
set /a tries=0
:wait_postgres_loop
set "PG="
for /f %%h in ('%DOCKER% inspect -f "{{.State.Health.Status}}" atlas-postgres 2^>nul') do set "PG=%%h"
if "!PG!"=="healthy" (
  echo         Postgres healthy.
  exit /b 0
)
set /a tries+=1
if !tries! geq 24 (
  echo         Postgres is not healthy after two minutes ^(status: !PG!^). Try: docker compose -f docker-compose.yml -f docker-compose.dev.yml logs postgres
  exit /b 1
)
call :sleep 5
goto :wait_postgres_loop

:start_service
set "STATE="
for /f "tokens=4" %%t in ('sc query %1 ^| findstr STATE') do set "STATE=%%t"
if "!STATE!"=="RUNNING" (
  echo         %1 already running.
  exit /b 0
)
if "!STATE!"=="PAUSED" (
  echo         %1 was paused by NSSM after a crash loop; resuming.
  sc continue %1 >nul 2>&1
  if not errorlevel 1 exit /b 0
  sc stop %1 >nul 2>&1
  call :sleep 3
)
echo         %1 is !STATE!; starting.
sc start %1 >nul 2>&1
exit /b 0

:wait_health
set /a tries=0
:wait_health_loop
curl -s -f -m 5 -o nul "http://127.0.0.1:8000/api/health?web=1" >nul 2>&1
if not errorlevel 1 exit /b 0
set /a tries+=1
if !tries! geq 20 (
  echo         no answer on port 8000 after about a minute. Service states:
  sc query AtlasWeb | findstr STATE
  sc query AtlasWorker | findstr STATE
  echo         look in logs\web.log and logs\web.service.log; scripts\deploy.bat reinstalls both services.
  exit /b 1
)
call :sleep 3
goto :wait_health_loop

rem ---------------------------------------------------------------- no service

:noservice
echo [atlas] no AtlasWeb service installed ^(scripts\deploy.bat installs it^); starting the dashboard window instead.
%DOCKER% compose -f docker-compose.yml -f docker-compose.dev.yml up -d
if errorlevel 1 (
  echo [atlas] docker compose failed. Is Docker Desktop running?
  goto :done
)
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul 2>&1
if errorlevel 1 (
  start "Atlas dashboard" /min .venv\Scripts\python.exe -m scripts.run_web
  call :sleep 6
)
start "" http://localhost:8000

:done
echo.
pause
endlocal
