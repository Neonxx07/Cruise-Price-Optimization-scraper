"""PyInstaller entry point.

This module bootstraps the entire system for standalone .exe distribution.
Build with:
    pyinstaller --onefile --name cruise-intel run.py
"""

import sys
import os

# Ensure the project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import main

if __name__ == "__main__":
    main()
