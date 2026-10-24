@echo off
title Daily Report Service
cd /d "%~dp0"

echo ============================================
echo   Daily Report Service
echo   Keep this window open while running.
echo   To stop: close this window or press Ctrl+C
echo ============================================
echo.

rem Prevent duplicate start: main.py already has single-instance file-lock.

".venv\Scripts\python.exe" main.py

echo.
echo Service stopped.
pause
