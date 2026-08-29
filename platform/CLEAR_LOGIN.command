#!/bin/bash
# Double-click this in Finder to run it in Terminal.app.
# If macOS refuses to open it the first time: right-click > Open, then
# confirm — or run once in Terminal: chmod +x CLEAR_LOGIN.command
set -e
cd "$(dirname "$0")"

VENV_DIR="$HOME/.cruiseintel_login_venv"
PYEXE="$VENV_DIR/bin/python3"

if [ ! -f "$PYEXE" ]; then
    echo "Python environment not found. Run SAVE_LOGIN.command first, then try this again."
    read -r -p "Press Enter to close..."
    exit 1
fi

"$PYEXE" clear_login.py
read -r -p "Press Enter to close..."
