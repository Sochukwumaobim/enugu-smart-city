@echo off
title Enugu Smart City — Starting...
color 0A
echo.
echo  ========================================================
echo   ENUGU SMART CITY — Smart Urban Management Platform
echo   Developed for MSc GIS Research, University of Nigeria
echo  ========================================================
echo.
echo  Starting all services. Please wait...
echo.

:: ── Check Python ─────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found.
    echo  Please install Python 3.9+ from https://python.org
    echo  Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)
echo  [OK] Python found

:: ── Install Python dependencies ───────────────────────────────────
echo  [..] Installing Python packages (first run only)...
pip install flask flask-cors geopandas rasterio scipy pandas pyproj ^
    traci sumolib --quiet --disable-pip-version-check 2>nul
echo  [OK] Python packages ready

:: ── Find a free port checker (use netstat) ────────────────────────
:: Kill any old instances on our ports
for /f "tokens=5" %%a in ('netstat -aon ^| find ":5000 "') do taskkill /f /pid %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find ":5001 "') do taskkill /f /pid %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find ":5002 "') do taskkill /f /pid %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8080 "') do taskkill /f /pid %%a >nul 2>&1

:: ── Get project root (parent of this bat file) ────────────────────
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

:: ── Check flood data exists ───────────────────────────────────────
if not exist "%ROOT%\data\flood_risk_results\flood_risk_buildings.geojson" (
    echo.
    echo  [..] Flood risk data not found. Running analysis...
    echo       This takes 5-10 minutes on first run.
    echo.
    cd /d "%ROOT%"
    python flood_analysis.py
    if errorlevel 1 (
        echo  [WARN] flood_analysis.py failed. Flood features may be limited.
    )
)

:: ── Start Backend 1: Routing API (port 5000) ─────────────────────
echo  [..] Starting routing API on port 5000...
start "Enugu Routing API" /min cmd /c "cd /d "%ROOT%\backend" && python app.py"
timeout /t 2 /nobreak >nul

:: ── Start Backend 2: Flood Risk API (port 5001) ──────────────────
echo  [..] Starting flood risk API on port 5001...
start "Enugu Flood API" /min cmd /c "cd /d "%ROOT%\backend" && python flood_api.py"
timeout /t 2 /nobreak >nul

:: ── Start Backend 3: Traffic API (port 5002) ─────────────────────
echo  [..] Starting traffic API on port 5002...
start "Enugu Traffic API" /min cmd /c "cd /d "%ROOT%\backend" && python traffic_api.py"
timeout /t 2 /nobreak >nul

:: ── Start Frontend HTTP Server (port 8080) ───────────────────────
echo  [..] Starting frontend server on port 8080...
start "Enugu Frontend" /min cmd /c "cd /d "%ROOT%\frontend" && python -m http.server 8080"
timeout /t 3 /nobreak >nul

:: ── Open browser ─────────────────────────────────────────────────
echo.
echo  ========================================================
echo   All services started!
echo.
echo   Opening browser at http://localhost:8080
echo.
echo   To stop everything: close this window or press Ctrl+C
echo   and close the 4 minimised terminal windows.
echo  ========================================================
echo.
start "" "http://localhost:8080"

:: Keep window open so user knows app is running
echo  Press any key to STOP all services and exit.
pause >nul

:: ── Cleanup on exit ───────────────────────────────────────────────
echo  Stopping all services...
for /f "tokens=5" %%a in ('netstat -aon ^| find ":5000 "') do taskkill /f /pid %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find ":5001 "') do taskkill /f /pid %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find ":5002 "') do taskkill /f /pid %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8080 "') do taskkill /f /pid %%a >nul 2>&1
echo  Done. Goodbye.
timeout /t 2 /nobreak >nul
