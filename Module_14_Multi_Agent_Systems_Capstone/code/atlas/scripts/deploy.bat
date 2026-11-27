@echo off
rem ============================================================================
rem Deploy Atlas: double-click this file. First install and every update.
rem Pulls master, installs, migrates, restarts the services, and publishes the
rem permanent link for the phone. Details and options: scripts\deploy.ps1.
rem ============================================================================
cd /d "%~dp0.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
