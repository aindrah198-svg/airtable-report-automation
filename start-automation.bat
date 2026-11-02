@echo off
cd /d "%~dp0"
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set REPORT_CHROME_CHANNEL=chrome
call .venv\Scripts\python.exe app.py
pause
