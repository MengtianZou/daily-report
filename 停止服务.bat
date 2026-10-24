@echo off
title Stop Daily Report Service
cd /d "%~dp0"

echo ============================================
echo   Stop Daily Report Service
echo ============================================
echo.

rem Kill only service processes (command line contains main.py),
rem never touch IDE debugger processes (debugpy).
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -match 'main\.py' -and $_.CommandLine -notmatch 'debugpy' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('[OK] Stopped PID ' + $_.ProcessId) }"

echo.
echo Service stopped. Press any key to exit.
pause >nul
