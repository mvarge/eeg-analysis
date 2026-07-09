# ============================================
# EEG Flanker Analysis - Run Script (PowerShell)
# ============================================
# Usage: .\run.ps1
# If you get an execution policy error, run once:
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# Don't treat stderr output from native commands (like python) as a
# terminating error. Python writes tracebacks to stderr for normal control
# flow (e.g. checking whether a module is importable), and PowerShell would
# otherwise abort the script.
$ErrorActionPreference = "Continue"
$PSNativeCommandUseErrorActionPreference = $false

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $ScriptDir "backend"
$VenvDir    = Join-Path $ScriptDir ".venv"

Write-Host ""
Write-Host "  ============================================"
Write-Host "       EEG Flanker Analysis Tool"
Write-Host "  ============================================"
Write-Host ""

# Create virtual environment if it doesn't exist
if (-not (Test-Path $VenvDir)) {
    Write-Host "-> Creating virtual environment..."
    python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "ERROR: Failed to create virtual environment." -ForegroundColor Red
        Write-Host "Make sure Python 3.10+ is installed and on your PATH."
        Write-Host "Download from https://www.python.org/downloads/"
        exit 1
    }
}

# Activate venv
& (Join-Path $VenvDir "Scripts\Activate.ps1")

# Install dependencies if needed. We redirect BOTH stdout and stderr to $null
# so PowerShell doesn't surface Python's ModuleNotFoundError traceback.
$null = & python -c "import fastapi" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "-> Installing dependencies (first run only)..."
    pip install -q -r (Join-Path $BackendDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to install dependencies." -ForegroundColor Red
        exit 1
    }
    Write-Host "   Dependencies installed"
}

Write-Host ""
Write-Host "-> Starting server..."
Write-Host "   Open http://localhost:8000 in your browser"
Write-Host "   Press Ctrl+C to stop"
Write-Host ""

Set-Location $BackendDir
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload
