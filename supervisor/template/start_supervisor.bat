@echo off
REM start_supervisor.bat - launches the supervisor loop and the Control Center together, each in its own console
REM window, then opens the browser. Close either window to stop that half. The loop restarts itself on Reload.
setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "SUPERVISOR_ROOT=%ROOT%"
start "supervisor-loop" powershell -NoExit -ExecutionPolicy Bypass -File "%ROOT%\tools\run_supervisor.ps1"
call "%ROOT%\start_control_center.bat"
endlocal
