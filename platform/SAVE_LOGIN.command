#!/bin/bash
# Double-click this in Finder to run it in Terminal.app.
# If macOS refuses to open it the first time: right-click > Open, then
# confirm — or run once in Terminal: chmod +x SAVE_LOGIN.command
set -e
cd "$(dirname "$0")"

VENV_DIR="$HOME/.cruiseintel_login_venv"
PYEXE="$VENV_DIR/bin/python3"

if [ ! -f "$PYEXE" ]; then
    echo "============================================================"
    echo " First-time setup - only happens once."
    echo "============================================================"
    if ! command -v python3 >/dev/null 2>&1; then
        echo "python3 not found. Install it (e.g. from python.org), then run this again."
        read -r -p "Press Enter to close..."
        exit 1
    fi
    python3 -m venv "$VENV_DIR"
    "$PYEXE" -m pip install --quiet --upgrade pip
    "$PYEXE" -m pip install --quiet keyring pydantic-settings
    echo "Setup complete."
fi

"$PYEXE" save_login.py
read -r -p "Press Enter to close..."
