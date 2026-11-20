"""Shared helper for `tests/test_migrate_attempts_dry_run.py` and `tests/test_migrate_attempts_apply_and_check.py`:
render a typer `--help` call at an explicit, pinned width, and normalise the captured text before asserting on
it, so neither depends on whatever terminal rich's own auto-detection would otherwise pick up from the ambient
process. CI's own runner renders `--help` as a colour/bold terminal (confirmed against a fresh CI run, job
135141732269) even once the obvious switches -- `FORCE_COLOR`, `TTY_COMPATIBLE` -- are explicitly unset on the
invocation itself (something about the real runner's own stdout still reads as a tty to rich's own fallback
`isatty()` check, unreproduced on this workstation); rich's own highlighter then wraps each hyphenated option
name's own matched segments (e.g. `--dry-run` as `-`, `-dry`, `-run`) in separate SGR escape codes, so the
literal substring the two tests assert never appears unbroken even though the option itself is listed, and even
though the colour channel itself is suppressed (`NO_COLOR` only suppresses colour, not the bold/dim styling the
highlighter also applies). Stripping every SGR escape code from the capture is the one normalisation that is
correct regardless of which switch controls the real cause: the printable characters of a hyphenated option
name are contiguous once the escape codes between them are gone. Not a test module itself (no `test_`/`_test`
name), so pytest does not collect it."""
from __future__ import annotations

import re

# Wide enough that rich never truncates or wraps an option's own name column.
# FORCE_COLOR/TTY_COMPATIBLE unset and NO_COLOR set: kept as a first line of defence (and sufficient on this
# workstation's own reproduction), even though CI's own runner needs the second line below too.
HELP_ENV = {"COLUMNS": "200", "LINES": "50", "FORCE_COLOR": None, "TTY_COMPATIBLE": None, "NO_COLOR": "1"}

_SGR_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def invoke_help(runner, app, *subcommand):
    """Invoke `<subcommand> --help` at the pinned width and colour above."""
    return runner.invoke(app, [*subcommand, "--help"], env=HELP_ENV)


def clean(output: str) -> str:
    """Strip every SGR escape code, so a hyphenated option name's own matched segments read as one
    contiguous substring regardless of whether rich decided to colour or style them."""
    return _SGR_ESCAPE.sub("", output)
