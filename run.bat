@echo off
echo ========================================
echo   Nabu Trader Listener - Running
echo ========================================
echo.
cd /d "%~dp0"
"YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" src\listener.py
pause
