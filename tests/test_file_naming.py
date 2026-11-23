"""R-27/F21: the suite "names its tests after review rounds" -- a test file called after the review pass that
found the defect (`test_review_1_12_0_r2_...py`) or the release that wrote it (`test_..._2_9_0.py`) rather than
what it tests. A finding's own identifier (`R-18`, `F9`, gate names `G1`-`G6`) is a stable, permanent label used
throughout this project's own documentation and is not what this guards against; a *round* or *release* number
baked into a file name is, because the next round or release makes the name wrong forever."""
from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

ROUND_OR_RELEASE_NUMBER = re.compile(r"review_\d+_\d+_\d+|_\d+_\d+_\d+\.py$")


def test_no_test_file_name_carries_a_review_round_or_release_number():
    offenders = sorted(p.name for p in TESTS_DIR.glob("test_*.py") if ROUND_OR_RELEASE_NUMBER.search(p.name))
    assert offenders == [], ("named after a review round or release, not its subject: " + ", ".join(offenders))
