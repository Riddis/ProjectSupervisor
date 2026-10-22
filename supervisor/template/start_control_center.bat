@echo off
REM start_control_center.bat - starts the Control Center UI for this project and opens it in the browser.
REM If the UI is already running, it just opens the browser. Close the "supervisor-ui" window to stop it.
REM At launch the UI lists framework updates on origin and asks whether to apply them (answer in that window).
REM To start the supervisor loop together with the UI, use start_supervisor.bat instead.
setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
for /f "tokens=2 delims=: " %%p in ('findstr /r /c:"^  port:" "%ROOT%\project.yaml"') do set "PORT=%%p"
if "%PORT%"=="" set "PORT=8790"
set "URL=http://127.0.0.1:%PORT%/"

if not exist "%PY%" (
    echo [ERROR] Python venv not found at "%PY%".
    echo Run tools\setup_env.ps1 first.
    pause
    exit /b 1
)

netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if %errorlevel%==0 (
    echo Control Center is already running - opening browser.
    start "" "%URL%"
    exit /b 0
)

echo Starting Control Center...
set "SUPERVISOR_ROOT=%ROOT%"
start "supervisor-ui" powershell -NoExit -ExecutionPolicy Bypass -File "%ROOT%\tools\run_control_center.ps1"

for /l %%i in (1,1,20) do (
    curl -s -o nul "%URL%" && goto :open
    timeout /t 1 /nobreak >nul
)
echo [WARN] UI did not respond within 20 s - check the "supervisor-ui" window for errors.

:open
start "" "%URL%"
endlocal
