@echo off
rem Builds dist\BOQ2RFQ\BOQ2RFQ.exe and dist\BOQ2RFQ.zip (ready to send).
cd /d "%~dp0.."
.venv\Scripts\python.exe -m pip install -q pyinstaller || exit /b 1
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\BOQ2RFQ.spec || exit /b 1
copy /y "packaging\README - START HERE.txt" "dist\BOQ2RFQ\" >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\BOQ2RFQ' -DestinationPath 'dist\BOQ2RFQ.zip'"
echo.
echo Done: dist\BOQ2RFQ.zip
