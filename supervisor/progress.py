"""Progress a running job reports about itself: `<run_dir>/PROGRESS.json`, one small object overwritten at each step
(`{"stage": "training", "detail": "step 300 of 500", "done": 300, "total": 500}`), so the Control Center can say
what a long job is doing (downloading, training, evaluating) instead of only how long it has run.

Anything may write it: a model session with its file tools (the brief asks it to, `instructions()`), a script that
session starts (it inherits `SUPERVISOR_PROGRESS_FILE` and can call `report()`), or a
deterministic process job. It is advisory and never trusted: it changes no state, it is not evidence for
verification, and a missing or malformed file just shows nothing. The loop logs a `job_progress` event when the
stage changes (`observe()`), never per step, so the log keeps one line per stage. Writing the file is also output
for the idle timer (supervisor.timeouts measures the run dir), so a job that reports progress is not idle."""
from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from pathlib import Path

from ._vendor.contract import atomic_json, read_json

PROGRESS_FILE = "PROGRESS.json"
ENV_VAR = "SUPERVISOR_PROGRESS_FILE"
MAX_STAGE = 80
MAX_DETAIL = 300


def path(run_dir) -> Path:
    return Path(run_dir) / PROGRESS_FILE


def env(spec: dict) -> dict:
    """The variable a launched job (and every process it starts) inherits, pointing at its own progress file."""
    run_dir = (spec or {}).get("run_dir")
    return {ENV_VAR: str(path(run_dir))} if run_dir else {}


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def report(stage: str, detail: str = "", done: float | None = None, total: float | None = None,
           file: str | os.PathLike | None = None) -> bool:
    """Write the current stage (for scripts: `from supervisor.progress import report`). `file` defaults to
    $SUPERVISOR_PROGRESS_FILE; without either it does nothing and returns False, so a script can call it
    unconditionally whether or not it runs under the supervisor."""
    target = file or os.environ.get(ENV_VAR)
    if not target:
        return False
    doc = {"stage": str(stage)[:MAX_STAGE], "detail": str(detail or "")[:MAX_DETAIL],
           "at": datetime.now(UTC).isoformat(timespec="seconds")}
    if _num(done) is not None:
        doc["done"] = done
    if _num(total) is not None:
        doc["total"] = total
    atomic_json(Path(target), doc)
    return True


def read(run_dir) -> dict | None:
    """The job's latest report, cleaned: {"stage", "detail", "done", "total", "percent", "updated"} (`updated` is
    the file's modification time, epoch seconds — the writer's own `at` is not trusted). None when there is none."""
    if not run_dir:
        return None
    p = path(run_dir)
    try:
        doc = read_json(p)
        updated = p.stat().st_mtime
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not str(doc.get("stage") or "").strip():
        return None
    done, total = _num(doc.get("done")), _num(doc.get("total"))
    if done is None and _num(doc.get("percent")) is not None:
        done, total = _num(doc.get("percent")), 100.0
    percent = max(0.0, min(100.0, done / total * 100)) if done is not None and total else None
    return {"stage": " ".join(str(doc["stage"]).split())[:MAX_STAGE],
            "detail": " ".join(str(doc.get("detail") or "").split())[:MAX_DETAIL],
            "done": done, "total": total, "percent": percent, "updated": updated}


def observe(layout, rec: dict) -> dict | None:
    """Called by the loop for a running job: logs `job_progress` when the reported stage differs from the last one
    logged for this attempt (kept on the job record as `progress_stage`, so a restarted loop does not log it again).
    Returns the current report. Never raises: progress is advisory, and a failure here must not fail the job."""
    from . import jobs as jobs_mod
    from . import tasks as tasks_mod
    current = read((rec.get("spec") or {}).get("run_dir"))
    if current is None or current["stage"] == rec.get("progress_stage"):
        return current
    try:
        jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], progress_stage=current["stage"])
        tasks_mod.log(layout, "job_progress", "loop", task=rec.get("task_id"), attempt=rec["attempt_id"],
                      stage=current["stage"], detail=current["detail"] or None)
    except (OSError, ValueError):
        pass
    return current


def instructions(run_dir) -> str:
    """The brief's paragraph: where to report and when."""
    p = path(run_dir)
    return (
        "## Progress reports\n\n"
        f"When a step will take more than a few minutes (a download, an install, a training run, a long test suite, "
        f"a batch of generations), write `{p}` before you start it and again when the stage changes, so the owner "
        "can see what you are doing. Overwrite the whole file each time with one JSON object: "
        '`{"stage": "training", "detail": "500 steps, batch 1", "done": 0, "total": 500}` — `stage` is a word or two '
        "(downloading, installing, training, evaluating, testing), `detail` one short line, `done`/`total` optional "
        "numbers. A script you start can report for you: it inherits the environment variable "
        f"`{ENV_VAR}`, and `from supervisor.progress import report; report(\"training\", \"step 300\", 300, 500)` "
        "writes the same file (it does nothing outside the supervisor); such a script may update `done` as often as "
        "is cheap. It is for the owner only and never evidence — the result JSON and the files are.")
