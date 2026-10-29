"""The reference plugin's job lane: a subprocess (so a ProcessAdapter) that writes the file the task asks for."""
from __future__ import annotations

import sys

from supervisor import plugin

# The child: write the output file, then result.json in the run folder (what ProcessAdapter collects).
WORKER = (
    "import json, pathlib, sys; run, out, rel = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]; "
    "out.parent.mkdir(parents=True, exist_ok=True); out.write_text('made by the reference lane\\n'); "
    "(run / 'result.json').write_text(json.dumps({'status': 'COMPLETE', 'artifacts': [rel]}))"
)


class ReferenceLane(plugin.ProcessAdapter):
    name = "reference_lane"

    def prepare(self, layout, task, attempt_id):
        job = plugin.job_spec(task)
        out = str(job.get("output") or "reference_out.txt")
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
                "root": str(layout.root), "timeout_s": 120,
                "argv": [sys.executable, "-c", WORKER, str(run_dir), str(layout.root / out), out]}

    def submit(self, spec):
        if plugin.config(plugin.Layout(spec["root"]), "reference").get("paused"):
            raise plugin.CapacityDeferred(0.0, "reference lane paused (config/routing.yaml reference.paused)")
        return super().submit(spec)


ADAPTER = ReferenceLane()            # one instance; setup() registers it (importing this module registers nothing)
