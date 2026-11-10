"""docs/plan/CONTRIBUTION_DRYRUN.md Gap 2: framework-change/SKILL.md step 5 told a contributor to add their
CHANGELOG entry "under `## Unreleased`" - a heading this repository's own CHANGELOG.md no longer carries once a
release has stamped it closed (test_changelog_stamp.py). Followed literally, past that stamp, the instruction
breaks the very gate it exists to help a contributor pass. The fix names the file's topmost heading instead of
a literal string that is not always there."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "framework-change" / "SKILL.md"


def _step_5() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("## 5. Gates before asking for review")
    end = text.index("## 6. Release and rollout")
    return text[start:end]


def test_step_5_does_not_tell_a_contributor_to_add_the_entry_under_a_literal_unreleased_heading():
    step = _step_5()
    assert "Then `CHANGELOG.md` under `## Unreleased`" not in step, (
        "step 5 still tells a contributor to add the CHANGELOG entry under a literal `## Unreleased` heading, "
        "which tests/test_changelog_stamp.py refuses once a release has stamped it closed"
    )


def test_step_5_names_the_topmost_heading_as_where_the_entry_goes():
    step = _step_5()
    assert "topmost heading" in step, "step 5 does not say the entry goes under the file's topmost heading"
