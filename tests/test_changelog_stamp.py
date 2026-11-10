"""The branch-opening half of CONTRIBUTOR_RULING.md section 8, ruling 2: the `## Unreleased` block holding
2.4.0's entries becomes `## 2.4.0 - <date>`, carrying one sentence recording that the tag was cut before the
heading moved, and this release's own entries open a fresh `## Unreleased` that becomes the topmost `## 2.5.0 -
<date>` heading at release. Pinning the exact dates against today is deliberately not asserted here - the dates
are a fact of when the stamping commits were made, not of when this test runs - only that both headings carry a
date in the same `YYYY-MM-DD` form as their neighbours."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_DATE = r"\d{4}-\d{2}-\d{2}"
_HEADING = re.compile(r"^##\s+(\d+\.\d+\.\d+|Unreleased)(?:\s+—\s+(" + _DATE + r"))?\s*$")


def _headings() -> list[tuple[str, str | None]]:
    out = []
    for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        m = _HEADING.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def test_the_2_5_0_heading_and_the_topmost_one_are_dated():
    """2.5.0 was the topmost heading at its release; a later release sits above it, so this pins that ## 2.5.0
    exists and stays dated, and that whatever heading is topmost carries a date too."""
    headings = _headings()
    version_headings = [(name, date) for name, date in headings if name != "Unreleased"]
    assert version_headings, "CHANGELOG.md has no version heading"
    name, date = version_headings[0]
    assert date, f"the topmost ## {name} heading carries no date in the YYYY-MM-DD form its neighbours use"
    assert dict(version_headings).get("2.5.0"), "no dated ## 2.5.0 heading exists"


def test_2_4_0_heading_is_dated_2026_11_08_and_sits_above_2_3_0():
    headings = _headings()
    version_names = [name for name, _ in headings if name != "Unreleased"]
    assert "2.4.0" in version_names, "no ## 2.4.0 heading exists"
    assert "2.3.0" in version_names, "no ## 2.3.0 heading exists"
    assert version_names.index("2.4.0") < version_names.index("2.3.0"), (
        "## 2.4.0 does not sit above ## 2.3.0"
    )
    by_name = dict(headings)
    assert by_name["2.4.0"] == "2026-11-08", f"## 2.4.0 is dated {by_name['2.4.0']!r}, not 2026-11-08"


def test_no_unreleased_block_still_holds_2_4_0s_six_entries():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    heading_lines = [i for i, line in enumerate(lines) if _HEADING.match(line)]
    assert heading_lines, "CHANGELOG.md has no heading at all"
    first_heading = lines[heading_lines[0]]
    assert first_heading.lstrip("#").strip() != "Unreleased", (
        "the file's first heading is still ## Unreleased - 2.4.0's entries were never stamped"
    )


def test_the_2_4_0_block_records_the_tag_was_cut_before_the_heading_moved():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _HEADING.match(line)]
    block_start = next(i for i in starts if _HEADING.match(lines[i]).group(1) == "2.4.0")
    later = [i for i in starts if i > block_start]
    block_end = later[0] if later else len(lines)
    block_text = "\n".join(lines[block_start:block_end]).lower()
    assert "tag" in block_text and "before" in block_text, (
        "the ## 2.4.0 block carries no sentence recording that the tag was cut before the heading moved"
    )
