@echo off
setlocal
cd /d "%~dp0"
title Stock Dashboard (Cloudflare Quick Tunnel)

rem ---------------------------------------------------------------------------
rem  Free remote access: no Cloudflare account, no domain.
rem  Runs the dashboard on 127.0.0.1 (not reachable on the network directly)
rem  and a Cloudflare "quick tunnel" that gives it a public HTTPS address like
rem  https://random-words.trycloudflare.com  -- a NEW address every start.
rem  The dashboard username/password (remote_login.bat) is the only lock,
rem  so Basic Auth is always on here. Use a long password.
rem ---------------------------------------------------------------------------

set "PORT=5000"
set "CF=cloudflared"
where cloudflared >nul 2>&1
if errorlevel 1 (
    if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" (
        set "CF=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
    ) else if exist "%ProgramFiles%\cloudflared\cloudflared.exe" (
        set "CF=%ProgramFiles%\cloudflared\cloudflared.exe"
    ) else (
        echo [ERROR] cloudflared is not installed: winget install Cloudflare.cloudflared
        goto :end
    )
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
if not defined DASH_USER goto :badlogin
if not defined DASH_PASS goto :badlogin

echo Starting the tunnel in a second window...
start "Cloudflare Tunnel - your address is in the box below" "%CF%" tunnel --no-autoupdate --url http://127.0.0.1:%PORT%

echo.
echo ============================================================
echo  Stock Dashboard - remote access via Cloudflare quick tunnel
echo  Your address appears in the "Cloudflare Tunnel" window, in a
echo  box like:   https://xxxx-xxxx-xxxx.trycloudflare.com
echo  It changes every time you run this file.
echo  Login user: %DASH_USER%
echo  Keep both windows open. Ctrl+C here stops the dashboard.
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
