@echo off
title Finaura Pro
:: Change to the folder where this .bat file lives (CRITICAL - fixes path errors)
cd /d "%~dp0"

echo.
echo ============================================================
echo   FINAURA PRO - Paper Trading Platform
echo ============================================================
echo.

:: Show Python version for debugging
python --version 2>&1
if errorlevel 1 (
    echo ERROR: Python not found! Download from https://www.python.org
    pause
    exit /b 1
)

:: Install dependencies (try --user first for permission issues, then without)
echo Installing dependencies...
pip install flask requests --user -q 2>nul || pip install flask requests -q 2>nul

echo Starting server...
echo.
python server.py
pause
