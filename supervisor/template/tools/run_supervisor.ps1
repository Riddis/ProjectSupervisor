# run_supervisor.ps1 - run the supervisor loop for this project; restart on reload (exit 3). Ctrl+C stops.
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root
$env:SUPERVISOR_ROOT = $Root
while ($true) {
  $env:PYTHONDONTWRITEBYTECODE = "1"
  New-Item -ItemType Directory -Force -Path (Join-Path $Root "runs\tmp") | Out-Null
  $env:TMP = Join-Path $Root "runs\tmp"; $env:TEMP = $env:TMP
  & $Py -B -m supervisor run
  $code = $LASTEXITCODE
  if ($code -eq 3) { Write-Host "reload requested - restarting"; Start-Sleep -Seconds 1; continue }
  Write-Host "supervisor exited with code $code"
  break
}
