@echo off
setlocal
cd /d "%~dp0"
title Stock Dashboard (Tailscale)

rem ---------------------------------------------------------------------------
rem  Starts the dashboard reachable ONLY over Tailscale (binds to this PC's
rem  Tailscale IP, not 0.0.0.0), with Basic Auth always on.
rem  Login is kept in remote_login.bat (git-ignored), created on first run.
rem ---------------------------------------------------------------------------

set "PORT=5000"
set "RULE=Stock Dashboard (Tailscale)"

rem --- 1. Find the Tailscale CLI ---------------------------------------------
set "TS=tailscale"
where tailscale >nul 2>&1
if errorlevel 1 (
    if exist "%ProgramFiles%\Tailscale\tailscale.exe" (
        set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
    ) else (
        echo [ERROR] Tailscale is not installed.
        echo         Download it from https://tailscale.com/download/windows ,
        echo         sign in, then run this file again.
        goto :end
    )
)

rem --- 2. Get this PC's Tailscale IPv4 address -------------------------------
set "TSIP="
for /f "usebackq delims=" %%i in (`"%TS%" ip -4 2^>nul`) do if not defined TSIP set "TSIP=%%i"
if not defined TSIP (
    echo [ERROR] Tailscale is installed but not connected.
    echo         Open the Tailscale app, log in / connect, then run this file again.
    goto :end
)

rem --- 3. Login for the dashboard (Basic Auth) -------------------------------
if not exist "%~dp0remote_login.bat" (
    echo First run: choose a username and password for the dashboard.
    echo Avoid these characters in the password:  ^& ^| ^< ^> ^^ %% "
    echo.
    set /p "NEWUSER=Username: "
    set /p "NEWPASS=Password: "
    call :savelogin
)
call "%~dp0remote_login.bat"
if not defined DASH_USER (
    echo [ERROR] remote_login.bat has no DASH_USER. Delete it and run again.
    goto :end
)
if not defined DASH_PASS (
    echo [ERROR] remote_login.bat has no DASH_PASS. Delete it and run again.
    goto :end
)

rem --- 4. Firewall rule (port %PORT%, Tailscale addresses only) ---------------
netsh advfirewall firewall show rule name="%RULE%" >nul 2>&1
if errorlevel 1 (
    netsh advfirewall firewall add rule name="%RULE%" dir=in action=allow protocol=TCP localport=%PORT% remoteip=100.64.0.0/10 >nul 2>&1
    if errorlevel 1 (
        echo [WARN] Could not add the Windows Firewall rule ^(needs admin^).
        echo        Right-click start_remote.bat -^> "Run as administrator" once,
        echo        or other devices may not be able to connect.
        echo.
    ) else (
        echo Added firewall rule "%RULE%" ^(port %PORT%, Tailscale only^).
    )
)

rem --- 5. Start ---------------------------------------------------------------
echo.
echo ============================================================
echo  Stock Dashboard - remote access via Tailscale
echo  On any device signed in to your Tailscale account, open:
echo.
echo      http://%TSIP%:%PORT%
echo.
echo  Login user: %DASH_USER%
echo  Keep this window open. Press Ctrl+C to stop.
echo ============================================================
echo.
python api_server.py --host %TSIP% --port %PORT%

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
