@echo off
setlocal
cd /d "%~dp0"

if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat
pip install -q -r requirements.txt

start "" venv\Scripts\pythonw.exe main.py

endlocal
