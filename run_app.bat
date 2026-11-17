@echo off
rem BOQ -> RFQ web app on this PC:  http://localhost:8080
rem (run_for_team.bat makes it reachable for colleagues on the office network)
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment ...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
)
.venv\Scripts\python.exe -c "import flask, waitress" 2>nul || .venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe server.py %*
