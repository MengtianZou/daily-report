@echo off
title Daily Report Dashboard
cd /d "%~dp0"

echo ============================================
echo   Daily Report Dashboard
echo   Browser will open automatically.
echo   Keep this window open while using it.
echo   To stop: close this window or press Ctrl+C
echo ============================================
echo.

rem 防止重复启动：已有看板实例在运行时直接提示退出（多实例会互相覆盖数据）
tasklist /FI "IMAGENAME eq python.exe" /FO CSV 2>nul | findstr /I "python.exe" >nul
if errorlevel 1 goto :run
wmic process where "name='python.exe' and commandline like '%%main.py%%'" get processid 2>nul | findstr /R "[0-9]" >nul
if not errorlevel 1 (
    echo [!] A dashboard instance is already running.
    echo     Please use the existing browser tab, or close it first.
    echo.
    pause
    exit /b 0
)

:run
".venv\Scripts\python.exe" main.py

echo.
echo Server stopped.
pause
