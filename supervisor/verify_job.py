"""Entry point of a tracked verify job: `python -m supervisor.verify_job <input.json>`.

The loop has already done the fast part of verification (artifacts present, containment, before/after scan) and
hands the slow part — re-running the task's checks and the worker's reported test commands — to this process, so the
supervisor never blocks on a test budget and PAUSE/DRAIN/STOP keep working while tests run. The result goes to
result.json in this job's own run dir (SUPERVISOR_RUN_DIR, the same env var any process job's adapter already sets —
ATTEMPT_MODEL.md §4.4, one less id to parse), as {"status": "COMPLETE", "verification": {"ok", "detail", "report"}};
a crash or timeout surfaces as a FAILED job and the loop records a failed verification with that reason."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import verify
from ._vendor.contract import atomic_json
from .paths import Layout


def run(input_path: Path) -> dict:
    spec = json.loads(Path(input_path).read_text(encoding="utf-8"))
    layout = Layout(Path(spec["root"]))
    to_run = list(spec.get("commands") or [])
    lim = verify.limits(layout)           # the tracked job honours the configured limits, no inline-path clamp (G3)
    ok, results = verify.run_declared_tests(layout.root, to_run, verify.allowed_test_commands(layout),
                                            tag=f"verify_{spec['task_id']}", roots=tuple(spec.get("roots") or ()),
                                            timeout_s=lim["per_command_timeout_s"], budget_s=lim["budget_s"],
                                            max_commands=lim["max_commands"], layout=layout)
    outcome = verify.commands_outcome(ok, results, list(spec.get("fixed") or []), bool(spec.get("fixed")),
                                      environment=verify.declared_environment(layout))
    return {"status": "COMPLETE", "verification": {"ok": outcome.ok, "detail": outcome.detail, "report": outcome.report},
            "digest": spec.get("digest"), "task_id": spec["task_id"]}


def main(argv: list[str]) -> int:
    input_path = Path(argv[1])
    run_dir = Path(os.environ["SUPERVISOR_RUN_DIR"])
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload = run(input_path)
    except Exception as exc:                      # noqa: BLE001 - report, never die silently
        payload = {"status": "FAILED", "error": f"verify job crashed: {type(exc).__name__}: {exc}"[:500]}
    atomic_json(run_dir / "result.json", payload)
    return 0 if payload.get("status") == "COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
