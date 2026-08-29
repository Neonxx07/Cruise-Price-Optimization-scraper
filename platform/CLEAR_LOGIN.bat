@echo off
setlocal
cd /d "%~dp0"

set PYEXE=venv\Scripts\python.exe

if not exist "%PYEXE%" (
    echo Python environment not found. Run SAVE_LOGIN.bat first, then try this again.
    pause
    exit /b 1
)

"%PYEXE%" clear_login.py
pause
