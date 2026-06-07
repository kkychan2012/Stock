@echo off
cd /d "%~dp0"
echo Starting Stock Dashboard...
echo Open browser at http://127.0.0.1:5000
echo Press Ctrl+C to stop.
echo.
python api_server.py
pause
