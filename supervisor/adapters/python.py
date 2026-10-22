"""In-process deterministic jobs. A task declares `job: {adapter: python, fn: <name>, params: {}}`;
`fn` must be registered in JOBS. Runs synchronously inside submit() (deterministic work is short); the handle carries
the finished result so a crash between submit and collect is harmless (the loop re-runs an unfinished attempt)."""
from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Callable

from .._vendor.contract import atomic_json
from ..store import now_iso
from .base import Adapter, Result, register


def job_spec(task) -> dict:
    extra = getattr(task, "model_extra", None) or {}
    return dict(extra.get("job") or {})

JOBS: dict[str, Callable[..., dict]] = {}


def job(name: str):
    def deco(fn):
        JOBS[name] = fn
        return fn
    return deco


@job("noop")
def _noop(**params):
    return {"status": "COMPLETE", "notes": "noop", "artifacts": []}


class PythonAdapter(Adapter):
    name = "python"

    def prepare(self, layout, task, attempt_id):
        jobspec = job_spec(task)
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {"adapter": "python", "task_id": task.id, "attempt_id": attempt_id, "fn": jobspec.get("fn", "noop"),
                "params": jobspec.get("params", {}), "run_dir": str(run_dir), "root": str(layout.root)}

    def submit(self, spec):
        fn = JOBS.get(spec["fn"])
        out = Path(spec["run_dir"]) / "result.json"
        if fn is None:
            payload = {"status": "FAILED", "error": f"unknown python job {spec['fn']!r}"}
        else:
            try:
                payload = fn(root=Path(spec["root"]), run_dir=Path(spec["run_dir"]), **spec["params"]) or {}
                payload.setdefault("status", "COMPLETE")
            except Exception as exc:  # noqa: BLE001 - job failures are results, not loop crashes
                payload = {"status": "FAILED", "error": f"{exc}", "traceback": traceback.format_exc()}
        payload["finished"] = now_iso()
        atomic_json(out, json.loads(json.dumps(payload, default=str)))
        return {"kind": "python", "result_path": str(out)}

    def recover(self, spec):
        out = Path(spec["run_dir"]) / "result.json"
        return {"kind": "python", "result_path": str(out)} if out.exists() else None

    def alive(self, handle):
        return False

    def collect(self, spec, handle):
        p = Path(handle["result_path"])
        if not p.exists():
            return Result(status="FAILED", error="python job left no result")
        payload = json.loads(p.read_text(encoding="utf-8"))
        return Result(status=payload.get("status", "FAILED"), artifacts=payload.get("artifacts", []),
                      notes=payload.get("notes", ""), error=payload.get("error"), raw=payload)


register(PythonAdapter())
