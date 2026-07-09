@echo off
REM ============================================
REM EEG Flanker Analysis - Run Script (Windows)
REM ============================================
REM Usage: run.bat
REM This installs dependencies (if needed) and starts the server.
REM Then open http://localhost:8000 in your browser.

setlocal

set "SCRIPT_DIR=%~dp0"
set "BACKEND_DIR=%SCRIPT_DIR%backend"
set "VENV_DIR=%SCRIPT_DIR%.venv"

echo.
echo   ============================================
echo        EEG Flanker Analysis Tool
echo   ============================================
echo.

REM Create virtual environment if it doesn't exist
if not exist "%VENV_DIR%" (
    echo -^> Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to create virtual environment.
        echo Make sure Python 3.10+ is installed and on your PATH.
        echo Download from https://www.python.org/downloads/
        exit /b 1
    )
)

REM Activate venv
call "%VENV_DIR%\Scripts\activate.bat"

REM Install dependencies if needed
python -c "import fastapi" 2>nul
if errorlevel 1 (
    echo -^> Installing dependencies ^(first run only^)...
    pip install -q -r "%BACKEND_DIR%\requirements.txt"
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies.
        exit /b 1
    )
    echo   Dependencies installed
)

echo.
echo -^> Starting server...
echo    Open http://localhost:8000 in your browser
echo    Press Ctrl+C to stop
echo.

cd /d "%BACKEND_DIR%"
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload

endlocal
