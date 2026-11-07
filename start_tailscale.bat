@echo off
setlocal
cd /d "%~dp0"
title Stock Dashboard (Tailscale Funnel)

rem ---------------------------------------------------------------------------
rem  Remote access with a FIXED address via Tailscale Funnel (free):
rem      https://<this-pc>.<tailnet>.ts.net/   (see: tailscale funnel status)
rem  Set DASH_URL in remote_login.bat to have it printed below.
rem  The dashboard runs on 127.0.0.1 only; Tailscale Funnel publishes it on the
rem  internet over HTTPS. Any browser can open it - nothing to install on the
rem  computer you connect from. The Funnel setting survives reboots (Tailscale
rem  runs as a Windows service); this script just re-applies it to be safe.
rem  The dashboard username/password (remote_login.bat) is the only lock, so
rem  Basic Auth is always on here. Use a long password.
rem  To stop publishing:  "C:\Program Files\Tailscale\tailscale.exe" funnel --https=443 off
rem ---------------------------------------------------------------------------

set "PORT=5000"
set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
if not exist "%TS%" (
    echo [ERROR] Tailscale is not installed: winget install Tailscale.Tailscale
    goto :end
)

if not exist "%~dp0remote_login.bat" (
    echo First run: choose a username and password for the dashboard.
    echo Use a LONG password - it is the only thing protecting the public address.
    echo Avoid these characters in the password:  ^& ^| ^< ^> ^^ %% "
    echo.
    set /p "NEWUSER=Username: "
    set /p "NEWPASS=Password: "
    call :savelogin
)
call "%~dp0remote_login.bat"
if not defined DASH_URL set "DASH_URL=(your Tailscale address - run: tailscale funnel status)"
if not defined DASH_USER goto :badlogin
if not defined DASH_PASS goto :badlogin

rem Already running (e.g. started by the "StockDashboard Start at Logon" task)?
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
    echo The dashboard is already running on port %PORT%:
    echo     %DASH_URL%   or   http://127.0.0.1:%PORT%
    echo Look for the minimized "Stock Dashboard" window on the taskbar.
    goto :end
)

"%TS%" funnel --bg %PORT% >nul
if errorlevel 1 (
    echo [WARNING] Could not turn on Tailscale Funnel. Is Tailscale logged in?
    echo           Check the Tailscale icon in the system tray.
)

echo.
echo ============================================================
echo  Stock Dashboard - remote access via Tailscale Funnel
echo  Address (always the same):
echo      %DASH_URL%
echo  Login user: %DASH_USER%
echo  Keep this window open. Ctrl+C here stops the dashboard.
echo ============================================================
echo.
python api_server.py --host 127.0.0.1 --port %PORT% --user "%DASH_USER%" --pass "%DASH_PASS%"
goto :end

:badlogin
echo [ERROR] remote_login.bat is missing DASH_USER/DASH_PASS. Delete it and run again.

:end
echo.
pause
endlocal
goto :eof

:savelogin
> "%~dp0remote_login.bat" echo @set "DASH_USER=%NEWUSER%"
>> "%~dp0remote_login.bat" echo @set "DASH_PASS=%NEWPASS%"
echo Saved to remote_login.bat ^(delete it to change the login^).
echo.
goto :eof
