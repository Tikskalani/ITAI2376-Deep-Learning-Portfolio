@echo off
rem ============================================================================
rem Atlas migration runner. Double-click to run, or invoke from any shell.
rem Walks db/migrations/*.sql in lexical order, runs each against the Postgres
rem container via the read-only /migrations mount in docker-compose.yml.
rem Each migration uses ON_ERROR_STOP=1 so the first failure aborts the whole run.
rem Idempotent if your migrations use `create ... if not exists` patterns.
rem ============================================================================
setlocal enabledelayedexpansion

rem Resolve project root as the parent of this script's directory.
cd /d "%~dp0.."

rem Sanity: the Postgres container must be up.
docker inspect -f "{{.State.Running}}" atlas-postgres >nul 2>&1
if errorlevel 1 (
  echo [migrate] atlas-postgres container is not running. Start it with: docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
  exit /b 1
)

set "MIGRATION_COUNT=0"
set "FAILED_FILE="

for %%f in (db\migrations\*.sql) do (
  set /a MIGRATION_COUNT+=1
  echo.
  echo [migrate] Running %%~nxf
  docker exec atlas-postgres psql -U atlas -d atlas -v ON_ERROR_STOP=1 -f /migrations/%%~nxf
  if errorlevel 1 (
    echo.
    echo [migrate] FAILED on %%~nxf
    set "FAILED_FILE=%%~nxf"
    goto :fail
  )
)

echo.
echo [migrate] OK. !MIGRATION_COUNT! migration(s) applied.
endlocal
exit /b 0

:fail
echo [migrate] Aborting. Fix !FAILED_FILE! and re-run.
endlocal
exit /b 1
