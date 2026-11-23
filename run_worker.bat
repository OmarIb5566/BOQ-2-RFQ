@echo off
rem BOQ -> RFQ background worker (use with BOQ_WORKER=external for the web server)
cd /d "%~dp0"
.venv\Scripts\python.exe worker.py %*
