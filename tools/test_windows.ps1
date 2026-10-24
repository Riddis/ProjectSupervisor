# test_windows.ps1 - run the framework test suite on Windows and save the output for review.
# The repository root is derived from this script's location, so the script works from any checkout
# (development tree, runtime worktree, or a copy) without editing.
#   powershell -ExecutionPolicy Bypass -File tools\test_windows.ps1 [-Python <path to python.exe>]
# Exit codes: pytest's own code when the suite ran; 2 when the results file could not be written (another run holds
# it, say) - a run whose log could not be written is not a validation run, whatever the console shows; 3 when the
# log ends without a pytest summary (the interpreter never ran pytest).
param([string]$Python = "")
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not $Python) {
    if (Test-Path "$Root\.venv\Scripts\python.exe") { $Python = "$Root\.venv\Scripts\python.exe" } else { $Python = "python" }
}
New-Item -ItemType Directory -Force -Path "$Root\runs\tmp" | Out-Null
$out = "$Root\runs\pytest_windows.txt"
# Every write to the results file is a terminating error when it fails: the previous run's text must never be shown
# as this run's result, and a busy or unwritable log ends the script with exit 2 before pytest starts.
try {
    "pytest $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') root=$Root python=$Python" | Set-Content -Encoding UTF8 -ErrorAction Stop $out
    "commit $(git -C $Root rev-parse --short HEAD) $(git -C $Root describe --tags --always --dirty)" | Out-File -Encoding UTF8 -Append -ErrorAction Stop $out
} catch {
    Write-Error "cannot write the results file ${out} ($($_.Exception.Message)); is another run holding it?"
    exit 2
}
$env:PYTHONDONTWRITEBYTECODE = "1"
# The suite's updater tests spawn `python -m supervisor` in a child: make this checkout importable for THIS run only
# (process-local; nothing is installed into the interpreter's environment and no other project is repointed).
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$Root;$env:PYTHONPATH" } else { "$Root" }
# AppData\Local\Temp can be access-denied for sandboxed agents; keep temp inside the repo (runs/ is gitignored).
$env:TMP = "$Root\runs\tmp"; $env:TEMP = "$Root\runs\tmp"
Get-ChildItem -Path $Root -Recurse -Directory -Filter "__pycache__" | Where-Object { $_.FullName -notlike "*\.venv\*" } | Remove-Item -Recurse -Force
$code = $null
try {
    & $Python -B -m pytest -q -p no:cacheprovider 2>&1 | Out-File -Encoding UTF8 -Append -ErrorAction Stop $out
    $code = $LASTEXITCODE
} catch {
    Write-Error "the results file ${out} became unwritable while the suite ran ($($_.Exception.Message)); this run is void"
    exit 2
}
$tail = Get-Content $out | Select-Object -Last 15
$tail
if (-not ($tail -match '\b(passed|failed|error|errors|no tests ran)\b')) {
    Write-Error "no pytest summary in ${out}: the suite did not run (python=$Python)"
    exit 3
}
if ($null -eq $code) { $code = 1 }
exit $code
