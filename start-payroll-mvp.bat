@echo off
title Chrisnat Payroll MVP
REM ============================================================
REM  Chrisnat Payroll MVP - local launcher
REM  Double-click this file to run the REAL app you and Claude
REM  have been building:
REM      K:\chrisnat-ops-system\chrisnat-payroll-mvp
REM
REM  NOTE: the "New folder" on your Desktop is a DIFFERENT, older
REM  project. Launching that one will NOT show our changes.
REM  FLASK_DEBUG=true means your edits show on a browser refresh,
REM  no relaunch needed.
REM ============================================================
cd /d "K:\chrisnat-ops-system\chrisnat-payroll-mvp"
set SKIP_DOTENV=true
set FLASK_DEBUG=true
set PORT=5000
echo.
echo Starting Chrisnat Payroll MVP ...
echo Open / refresh in your browser:  http://127.0.0.1:5000
echo Keep this window open while using the app. Press Ctrl+C to stop.
echo If you see "address already in use", an old copy (e.g. the WSL one)
echo is still running - close that first, then run this again.
echo. 
start "" http://127.0.0.1:5000
where python >nul 2>nul
if %ERRORLEVEL%==0 ( python run.py ) else ( py -3 run.py )
pause
