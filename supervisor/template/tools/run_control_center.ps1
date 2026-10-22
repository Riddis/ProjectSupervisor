# run_control_center.ps1 - run the Control Center for this project; restart on exit 3 (a framework update was applied
# and the UI must re-import the new code). Ctrl+C stops. Used by start_control_center.bat.
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root
$env:SUPERVISOR_ROOT = $Root
while ($true) {
  $env:PYTHONDONTWRITEBYTECODE = "1"
  New-Item -ItemType Directory -Force -Path (Join-Path $Root "runs\tmp") | Out-Null
  $env:TMP = Join-Path $Root "runs\tmp"; $env:TEMP = $env:TMP
  & $Py -B -m supervisor ui
  $code = $LASTEXITCODE
  if ($code -eq 3) { Write-Host "restarting the Control Center on the updated framework"; Start-Sleep -Seconds 1; continue }
  Write-Host "Control Center exited with code $code"
  break
}
