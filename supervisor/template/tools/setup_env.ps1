# setup_env.ps1 - create this project's Python 3.11 venv and install the Supervisor framework into it (editable,
# from a local clone). Idempotent; safe to re-run.
#   powershell -ExecutionPolicy Bypass -File tools\setup_env.ps1 [-Framework D:\path\to\Supervisor]
# Without -Framework the script looks for SUPERVISOR_FRAMEWORK, then a `Supervisor` folder next to this project.
param([string]$Framework = "")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $Framework) { $Framework = $env:SUPERVISOR_FRAMEWORK }
if (-not $Framework) { $Framework = Join-Path (Split-Path -Parent $Root) "Supervisor" }
if (-not (Test-Path (Join-Path $Framework "pyproject.toml"))) {
  throw "Supervisor framework not found at '$Framework'. Clone it and pass -Framework <path> (or set SUPERVISOR_FRAMEWORK)."
}

$py = & py -3.11 -c "import sys;print(sys.executable)" 2>$null
if (-not $py) { throw "Python 3.11 not found via 'py -3.11'. Install it from python.org and re-run." }
Write-Host "using $py"
$vpy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { & py -3.11 -m venv (Join-Path $Root ".venv") }
& $vpy -m pip install --upgrade pip wheel | Out-Null
& $vpy -m pip install -e "$Framework[ui]"

[Environment]::SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "1", "User")
New-Item -ItemType Directory -Force -Path (Join-Path $Root "runs\tmp") | Out-Null
Write-Host ""
Write-Host "framework installed from $Framework"
& $vpy -m supervisor version
& $vpy -m supervisor doctor
Write-Host ""
Write-Host "Next: start_control_center.bat (UI only) or start_supervisor.bat (loop + UI)."
