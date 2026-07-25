@echo off
setlocal
cd /d "%~dp0"

rem The Python environment lives outside this folder on purpose: this
rem project's own path is deeply nested, and one of the GUI's packages
rem (PySide6) ships files with names too long to install under it on
rem Windows (MAX_PATH). C:\cruisevenv is short and avoids that.
set VENV_DIR=C:\cruisevenv\venv
set PYEXE=%VENV_DIR%\Scripts\python.exe

if not exist "%PYEXE%" (
    echo ============================================================
    echo  First-time setup - only happens once. May take a few
    echo  minutes ^(downloading Python packages + a browser^)...
    echo ============================================================
    py -3.14 -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo.
        echo Could not create the Python environment. Is Python 3.14 installed?
        echo ^(Check with: py -0^)
        pause
        exit /b 1
    )

    "%PYEXE%" -m pip install --quiet --upgrade pip
    "%PYEXE%" -m pip install --quiet -r requirements.txt PySide6 qasync
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

echo Starting CruiseIntel Desktop Scanner...
"%PYEXE%" -m gui.main
if errorlevel 1 (
    echo.
    echo The app closed with an error - see above for details.
    pause
)
