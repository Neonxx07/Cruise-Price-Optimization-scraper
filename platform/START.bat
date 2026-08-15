@echo off
setlocal
cd /d "%~dp0"

set PYEXE=venv\Scripts\python.exe

if not exist "%PYEXE%" (
    echo ============================================================
    echo  First-time setup - only happens once. May take a few
    echo  minutes ^(downloading Python packages + a browser^)...
    echo ============================================================
    python -m venv venv
    if errorlevel 1 (
        echo.
        echo Could not create the Python environment. Is Python installed?
        echo Get it from https://www.python.org/downloads/ ^(check "Add to PATH"^).
        pause
        exit /b 1
    )

    "%PYEXE%" -m pip install --quiet --upgrade pip
    "%PYEXE%" -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Failed to install dependencies. Check your internet connection and try again.
        pause
        exit /b 1
    )

    "%PYEXE%" -m playwright install chromium
    if errorlevel 1 (
        echo.
        echo Failed to install the browser used for scanning. Check your internet connection and try again.
        pause
        exit /b 1
    )

    echo Setup complete.
)

"%PYEXE%" easy_menu.py
pause
