# test_focused.ps1 - run a chosen pytest target in this worktree and save the output for review.
# Mirrors test_windows.ps1's temp/PYTHONPATH setup but forwards extra args, for a task's own focused runs.
#   powershell -ExecutionPolicy Bypass -File tools\test_focused.ps1 -Python <python.exe> -Out <file> -Args "<pytest args>"
param([string]$Python = "", [string]$Out = "", [string]$PytestArgs = "")
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not $Python) {
    if (Test-Path "$Root\.venv\Scripts\python.exe") { $Python = "$Root\.venv\Scripts\python.exe" } else { $Python = "python" }
}
if (-not $Out) { $Out = "$Root\runs\pytest_focused.txt" }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Out) | Out-Null
New-Item -ItemType Directory -Force -Path "$Root\runs\tmp" | Out-Null
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$Root;$env:PYTHONPATH" } else { "$Root" }
$env:TMP = "$Root\runs\tmp"; $env:TEMP = "$Root\runs\tmp"
"pytest $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') root=$Root python=$Python args=$PytestArgs" | Set-Content -Encoding UTF8 $Out
"commit $(git -C $Root rev-parse --short HEAD) $(git -C $Root describe --tags --always --dirty)" | Out-File -Encoding UTF8 -Append $Out
$argList = @("-B", "-m", "pytest", "-q", "-p", "no:cacheprovider") + ($PytestArgs -split ' ' | Where-Object { $_ })
& $Python @argList 2>&1 | Out-File -Encoding UTF8 -Append $Out
$code = $LASTEXITCODE
Get-Content $Out | Select-Object -Last 15
exit $code
