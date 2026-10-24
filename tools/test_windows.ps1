# test_windows.ps1 - run the framework test suite on Windows and save the output for review.
# The repository root is derived from this script's location, so the script works from any checkout
# (development tree, runtime worktree, or a copy) without editing.
#   powershell -ExecutionPolicy Bypass -File tools\test_windows.ps1 [-Python <path to python.exe>]
param([string]$Python = "")
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not $Python) {
    if (Test-Path "$Root\.venv\Scripts\python.exe") { $Python = "$Root\.venv\Scripts\python.exe" } else { $Python = "python" }
}
New-Item -ItemType Directory -Force -Path "$Root\runs\tmp" | Out-Null
$out = "$Root\runs\pytest_windows.txt"
"pytest $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') root=$Root python=$Python" | Set-Content -Encoding UTF8 $out
"commit $(git -C $Root rev-parse --short HEAD) $(git -C $Root describe --tags --always --dirty)" | Out-File -Encoding UTF8 -Append $out
$env:PYTHONDONTWRITEBYTECODE = "1"
# The suite's updater tests spawn `python -m supervisor` in a child: make this checkout importable for THIS run only
# (process-local; nothing is installed into the interpreter's environment and no other project is repointed).
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$Root;$env:PYTHONPATH" } else { "$Root" }
# AppData\Local\Temp can be access-denied for sandboxed agents; keep temp inside the repo (runs/ is gitignored).
$env:TMP = "$Root\runs\tmp"; $env:TEMP = "$Root\runs\tmp"
Get-ChildItem -Path $Root -Recurse -Directory -Filter "__pycache__" | Where-Object { $_.FullName -notlike "*\.venv\*" } | Remove-Item -Recurse -Force
& $Python -B -m pytest -q -p no:cacheprovider 2>&1 | Out-File -Encoding UTF8 -Append $out
$code = $LASTEXITCODE
Get-Content $out | Select-Object -Last 15
exit $code
