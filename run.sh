#!/bin/bash
# ============================================
# EEG Flanker Analysis — Run Script
# ============================================
# Usage: ./run.sh
# This installs dependencies (if needed) and starts the server.
# Then open http://localhost:8000 in your browser.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
VENV_DIR="$SCRIPT_DIR/.venv"

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║     EEG Flanker Analysis Tool        ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# Create virtual environment if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "→ Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Activate venv
source "$VENV_DIR/bin/activate"

# Install dependencies if needed
if ! python -c "import fastapi" 2>/dev/null; then
    echo "→ Installing dependencies (first run only)..."
    pip install -q -r "$BACKEND_DIR/requirements.txt"
    echo "  ✓ Dependencies installed"
fi

# Free port 8000 from any stale server. A crash or an incomplete shutdown can
# leave an old uvicorn process alive still holding the port, so a fresh run
# would fail to bind and keep serving the OLD code. Kill any lingering
# listener first so every restart runs the freshly pulled code.
echo "→ Checking for a previous server on port 8000..."
STALE_PIDS="$(lsof -ti:8000 2>/dev/null || true)"
if [ -n "$STALE_PIDS" ]; then
    echo "  Stopping stale server (PID $STALE_PIDS)..."
    # shellcheck disable=SC2086
    kill $STALE_PIDS 2>/dev/null || true
    sleep 1
    STILL="$(lsof -ti:8000 2>/dev/null || true)"
    if [ -n "$STILL" ]; then
        # shellcheck disable=SC2086
        kill -9 $STILL 2>/dev/null || true
        sleep 1
    fi
fi

echo ""
echo "→ Starting server..."
echo "  Open http://localhost:8000 in your browser"
echo "  Press Ctrl+C to stop"
echo ""

cd "$BACKEND_DIR"
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload
