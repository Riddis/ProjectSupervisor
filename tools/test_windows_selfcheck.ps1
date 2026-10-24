# test_windows_selfcheck.ps1 - verifies test_windows.ps1 fails loudly when its results file cannot be written
# (review finding on 1.8.1: a busy log used to show the previous run and exit 0). Holds runs\pytest_windows.txt
# open with no sharing, runs the script, and expects exit 2 without the suite starting. Run it from the checkout:
#   powershell -ExecutionPolicy Bypass -File tools\test_windows_selfcheck.ps1
$Root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force -Path "$Root\runs" | Out-Null
$out = "$Root\runs\pytest_windows.txt"
$before = if (Test-Path $out) { Get-Content $out -Raw } else { "" }
$handle = [System.IO.File]::Open($out, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$Root\tools\test_windows.ps1" -Python "python" 2>$null | Out-Null
    $code = $LASTEXITCODE
} finally {
    $handle.Close()
}
$after = if (Test-Path $out) { Get-Content $out -Raw } else { "" }
if ($code -ne 2) { Write-Error "expected exit 2 with the results file held open, got $code"; exit 1 }
if ($after -ne $before) { Write-Error "the held results file changed; the script must not have written to it"; exit 1 }
"OK: test_windows.ps1 exits 2 when the results file is busy and leaves it untouched"
exit 0
