@echo off
echo ========================================
echo   Nabu Trader Listener - Auth
echo ========================================
echo.
set /p PHONE="Enter your phone number (e.g. +6281234567890): "
echo.
echo Sending code to %PHONE%...
"YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" "%~dp0auth.py" %PHONE%
echo.
pause
