"""R-28: five small defects the review listed and nothing had owned.

1. `runtime.is_live`'s fixed 180s heartbeat window reads a turn that is still legitimately blocking (a repo-wide
   snapshot, a git commit) as an offline loop, and the Overview then enables Start -- clicking it while the loop is
   still running races a second process against the first.
2. `verify.snapshot` hashes the whole working tree once at dispatch (the `before` manifest) and once more at
   verification (the `after` manifest, inside `verify()`) -- two different points in time, both needed for the
   before/after diff, never the same work done twice. Nothing to fix; documented in code and here.
3. `loop.RENDERED_PROJECTIONS` and `tasks.VOLATILE_PROJECTIONS` are literal copies of `verify.EXCLUDED_FILES`,
   each then unioned at its one call site with `verify.excluded_files()` -- which already contains `EXCLUDED_FILES`.
4. Three routing loaders (`gate.routing_doc`, `adapters.claude_cli.routing`, `loop.scheduler_routing`) and about a
   dozen inline `yaml.safe_load(cfg.read_text())` copies each reimplement "read config/routing.yaml, {} when
   absent or malformed".
5. `/tasks?phase=<value>` uses `phase.isdigit()` then `int(phase)`; several Unicode digit classes satisfy
   `isdigit()` but not `int()` (they are `Numeric_Type=Digit`, not `Decimal`), so a superscript, subscript or
   circled digit in the query string raises instead of being ignored.
"""
from __future__ import annotations

import ast
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import supervisor
from supervisor import runtime
from supervisor.ui.app import create_app
from supervisor.ui.routes.overview import controls

SUPERVISOR_ROOT = Path(supervisor.__file__).resolve().parent

# ---------------------------------------------------------------- 1. heartbeat window vs. a long blocking turn


def test_a_turn_that_blocks_longer_than_the_fixed_window_is_not_reported_offline(layout):
    """The previous turn took 220s (a slow snapshot+commit); this turn is still inside it, 200s since the last
    heartbeat -- past the old fixed 180s window, but well inside a window that scales with the last turn's own
    length. A loop that is actually dead never had a turn that long, so this does not mask a real stall."""
    runtime.write(layout, status="RUNNING", pid=12345, heartbeat_epoch=time.time() - 200, turn_s=220)
    assert runtime.is_live(layout), "a turn still inside its own last known duration must not read as offline"


def test_start_is_not_enabled_while_that_long_turn_is_still_running(layout):
    runtime.write(layout, status="RUNNING", pid=12345, heartbeat_epoch=time.time() - 200, turn_s=220)
    rt = runtime.read(layout)
    live = runtime.is_live(layout)
    start = next(r for r in controls(live, rt) if r["action"] == "start")
    assert start["enabled"] is False, "Start must stay disabled while the loop is still live"


def test_a_genuinely_dead_loop_still_reads_offline(layout):
    """A legacy-shaped document (no {pid, create_time} recorded -- the fallback path, R-09's turn_s=2.0 pattern
    pre-dates it) with an ordinary last turn and a heartbeat now far older than that must still read offline --
    the fix must not make every stall silent."""
    runtime.write(layout, status="RUNNING", pid=12345, heartbeat_epoch=time.time() - 400, turn_s=2.0)
    assert not runtime.is_live(layout)


def test_a_dead_process_reads_offline_even_right_after_a_long_turn(layout):
    """The window-scaling fix alone has no ceiling: a turn that legitimately ran a whole verification
    (verify.TEST_BUDGET_S = 900s) would read live for that long doubled even after the process backing it is
    gone. `heartbeat()` records {pid, create_time}; `is_live()` must believe a document that records a real pid
    only while that exact process is still running, not however old the window math would otherwise allow. 999999
    with create_time=1.0 is the exited-process identity already used in tests/test_leases.py."""
    runtime.write(layout, status="RUNNING", pid=999999, create_time=1.0,
                  heartbeat_epoch=time.time() - 30, turn_s=900.0)
    assert not runtime.is_live(layout), "a dead {pid, create_time} must never read as live, no matter the window"


def test_the_writing_process_itself_reads_live_after_a_long_turn(layout):
    """The positive side of the same check: the real {pid, create_time} `heartbeat()` records for the process
    actually running this test is still alive, so a long recorded turn is read as live by process liveness, not
    by a window that could otherwise be made to lie."""
    runtime.heartbeat(layout, "RUNNING", turn_s=900.0)
    runtime.write(layout, heartbeat_epoch=time.time() - 30)      # age the heartbeat; pid/create_time stay this test's own
    assert runtime.is_live(layout)


# ---------------------------------------------------------------- 2. the whole-tree hash: once per dispatch, once
# per verification -- not a defect, kept as a guard against a future third call creeping in.


def test_snapshot_is_taken_at_most_twice_for_one_writing_delivery(layout, monkeypatch):
    from supervisor import testing
    from supervisor.loop import Supervisor
    from tests.conftest import make_task
    from supervisor import verify as verify_mod

    calls = []
    real_snapshot = verify_mod.snapshot

    def wrapped(lay, task):
        calls.append(task.id)
        return real_snapshot(lay, task)

    monkeypatch.setattr(verify_mod, "snapshot", wrapped)
    make_task(layout, "A", job={"adapter": "fake_lane"}, artifacts=["out.txt"], owned_paths=["."])
    with testing.lane(testing.FakeLane("fake_lane", artifacts=["out.txt"])):
        sup = Supervisor(layout, sleep_s=0)
        for _ in range(6):
            sup.once()
            if tasks_mod_status(layout) == "COMPLETE":
                break
    # Exactly 2, not "at most 2": the floor guards the evidence the verifier diffs (lose the before-dispatch
    # snapshot and this would still pass at 0). The two calls are Supervisor.dispatch's before_manifest.json
    # (loop.py) and verify.verify()'s after snapshot (verify.py) -- different points in time, never the same
    # work done twice.
    assert len(calls) == 2, f"expected exactly 2 (before-dispatch, after-delivery), got {len(calls)} for task A: {calls}"


def tasks_mod_status(layout):
    from supervisor import tasks as tasks_mod
    return tasks_mod.load(layout, "A").status.value


# ---------------------------------------------------------------- 3. one projection file set, not three


_PROJECTION_FILES = {"STATE.md", "TASKS.md", "ISSUES.md", "DECISIONS.md", "HUMAN_INBOX.md", "OWNER_TASKS.md"}


def _literal_set_definitions(target: set[str]) -> list[str]:
    hits = []
    for path in SUPERVISOR_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Set):
                values = {elt.value for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}
                if values == target:
                    hits.append(f"{path.relative_to(SUPERVISOR_ROOT)}:{node.lineno}")
    return hits


def test_the_projection_file_set_has_exactly_one_definition():
    hits = _literal_set_definitions(_PROJECTION_FILES)
    assert len(hits) == 1, f"the rendered-projection file set is defined {len(hits)} times, expected 1: {hits}"


# ---------------------------------------------------------------- 4. one routing loader, one configuration reader


_INLINE_READ_RE = re.compile(r'yaml\.safe_load\(\s*\w+\.read_text\(')


def _routing_yaml_safe_load_functions() -> list[str]:
    """Every function body (outside routing_config.py) that both constructs `config/routing.yaml`'s own path and
    parses it with a direct `yaml.safe_load(<name>.read_text(...))` -- the signature of an inline reader
    reimplementing the one loader's own job. A function that reads the *raw text* into a variable first (a
    migration's text-preserving edit, Settings' own read-modify-write) is a different, legitimate concern and
    does not match: it keeps the original bytes to write back, which `routing_config.load()` -- {} on any error,
    no text kept -- cannot do for it.

    This matches today's idiom textually (the exact `config / "routing.yaml"` spelling and a direct
    `yaml.safe_load(x.read_text(` call), not the behaviour in general: a reader that built the path through a
    variable or a layout helper, used `open()`, or called `yaml.load`/`yaml.full_load` would not be caught. It is
    a ratchet against a new copy of this one pattern, not a guarantee the invariant holds for every possible
    inline reader -- see docs/releases/2.9.0/R-28-smaller-items.md."""
    hits = []
    for path in SUPERVISOR_ROOT.rglob("*.py"):
        if path.name == "routing_config.py":
            continue
        text = path.read_text(encoding="utf-8")
        if 'routing.yaml"' not in text or "yaml.safe_load" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        lines = text.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start, end = node.lineno - 1, getattr(node, "end_lineno", node.lineno)
                body = "\n".join(lines[start:end])
                if "config / \"routing.yaml\"" in body and _INLINE_READ_RE.search(body):
                    hits.append(f"{path.relative_to(SUPERVISOR_ROOT)}:{node.name}")
    return hits


def test_routing_yaml_is_parsed_in_exactly_one_place():
    hits = _routing_yaml_safe_load_functions()
    assert hits == [], f"inline config/routing.yaml readers outside routing_config.py: {hits}"


# ---------------------------------------------------------------- 5. the phase filter and non-ASCII digits


@pytest.mark.parametrize("value", ["²", "₂", "①"], ids=["superscript", "subscript", "circled"])
def test_tasks_page_ignores_a_non_ascii_digit_phase_instead_of_raising(layout, value):
    from tests.conftest import make_task
    make_task(layout, "A")       # `int(phase)` inside the list comprehension only runs once there is a task to test
    client = TestClient(create_app(layout, update_check=False), base_url="http://127.0.0.1")
    resp = client.get("/tasks", params={"phase": value})
    assert resp.status_code == 200
