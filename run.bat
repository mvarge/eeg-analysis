@echo off
REM ============================================
REM EEG Flanker Analysis - Run Script (Windows)
REM ============================================
REM Double-click this file in Explorer to launch the app.
REM It creates a Python virtualenv the first time, installs
REM dependencies, opens your browser, and starts the server.

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "BACKEND_DIR=%SCRIPT_DIR%backend"
set "VENV_DIR=%SCRIPT_DIR%.venv"

title EEG Flanker Analysis Tool

echo.
echo   ============================================
echo        EEG Flanker Analysis Tool
echo   ============================================
echo.

REM ---- Locate a working Python interpreter --------------------
set "PYTHON_EXE="
for %%C in (python py python3) do (
    if not defined PYTHON_EXE (
        %%C --version >nul 2>nul
        if not errorlevel 1 set "PYTHON_EXE=%%C"
    )
)

if not defined PYTHON_EXE (
    echo.
    echo ERROR: Python was not found on your system.
    echo.
    echo Please install Python 3.10 or newer from:
    echo     https://www.python.org/downloads/
    echo During installation tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

REM ---- Create virtual environment if needed -------------------
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment ^(first run only^)...
    %PYTHON_EXE% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to create virtual environment.
        echo Make sure Python 3.10+ is installed correctly.
        echo.
        pause
        exit /b 1
    )
)

REM ---- Activate venv ------------------------------------------
call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
    echo.
    echo ERROR: Failed to activate virtual environment.
    echo Try deleting the .venv folder and running again.
    echo.
    pause
    exit /b 1
)

REM ---- Install dependencies if needed -------------------------
python -c "import fastapi, mne, pywt" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies ^(first run only, ~30 seconds^)...
    pip install -q -r "%BACKEND_DIR%\requirements.txt"
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies.
        echo Check your internet connection and try again.
        echo.
        pause
        exit /b 1
    )
    echo   Dependencies installed.
)

REM ---- Free port 8000 from any stale server -------------------
REM Ctrl+C in a .bat window often leaves the child python/uvicorn
REM process alive, still holding the port. A fresh run would then
REM fail to bind and you'd keep serving the OLD code. Kill any
REM lingering listener on 8000 before starting, so every restart
REM guarantees the freshly pulled code is what actually runs.
echo Checking for a previous server on port 8000...
set "STALE_PIDS="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do (
    if not "%%P"=="0" (
        echo   Stopping stale server ^(PID %%P^)...
        taskkill /F /PID %%P >nul 2>nul
        set "STALE_PIDS=1"
    )
)
if defined STALE_PIDS (
    REM give Windows a moment to release the socket
    timeout /t 1 /nobreak >nul
)

REM ---- Open browser after a short delay -----------------------
echo.
echo Starting server on http://localhost:8000
echo Press Ctrl+C in this window to stop the app.
echo.
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8000"

REM ---- Start the server ---------------------------------------
cd /d "%BACKEND_DIR%"
python -m uvicorn server:app --host 127.0.0.1 --port 8000

REM ---- Server stopped — keep window open ----------------------
echo.
echo Server stopped.
pause
endlocal
