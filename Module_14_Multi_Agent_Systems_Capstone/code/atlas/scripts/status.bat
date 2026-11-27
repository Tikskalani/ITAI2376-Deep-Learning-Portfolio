@echo off
rem ============================================================================
rem Atlas status: services, health, Claude, and the phone link. No admin needed.
rem ============================================================================
setlocal
cd /d "%~dp0.."
echo.
echo [atlas] services
for %%s in (AtlasWeb AtlasWorker) do (
  sc query %%s >nul 2>&1
  if errorlevel 1 (
    echo    %%s: not installed ^(run scripts\deploy.bat^)
  ) else (
    for /f "tokens=4" %%t in ('sc query %%s ^| findstr STATE') do echo    %%s: %%t
  )
)
echo.
echo [atlas] health ^(version, database, schema, worker, Claude^)
curl -s --max-time 30 "http://127.0.0.1:8000/api/health?deep=1"
if errorlevel 1 echo    the web service is not answering on port 8000
echo.
echo.
if exist logs\public_url.txt (
  set /p URL=<logs\public_url.txt
  call echo [atlas] phone link: %%URL%%
)
echo.
echo [atlas] logs: logs\web.log  logs\worker.log
endlocal
pause
