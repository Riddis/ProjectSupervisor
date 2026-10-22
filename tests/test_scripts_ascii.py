"""Windows PowerShell 5 reads unmarked .ps1 files as ANSI; any non-ASCII byte can break a string literal
(it did: an em-dash in run_supervisor.ps1). Keep the scripts pure ASCII."""
import pathlib


def test_powershell_scripts_are_ascii():
    root = pathlib.Path(__file__).resolve().parents[1] / "supervisor" / "template"
    bad = {}
    for p in sorted((root / "tools").glob("*.ps1")):
        data = p.read_bytes()
        offenders = [i for i, b in enumerate(data) if b > 0x7F]
        if offenders:
            bad[p.name] = offenders[:3]
    assert not bad, f"non-ASCII bytes in PowerShell scripts: {bad}"
