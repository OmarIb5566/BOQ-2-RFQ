@echo off
rem BOQ -> RFQ web app for the whole team: colleagues open http://<this PC's name or IP>:8080
rem Windows may ask once to allow Python through the firewall - allow it on private networks.
cd /d "%~dp0"
call run_app.bat --lan %*
