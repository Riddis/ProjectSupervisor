"""1.13.0, a skill-trial finding (task-author): a `process` job must write `result.json` into its run folder, and it
could find that folder only as the parent of `$SUPERVISOR_PROGRESS_FILE`, which nothing documented. The adapter now
also sets `SUPERVISOR_RUN_DIR`, the folder itself."""
from __future__ import annotations

import sys

from supervisor import tasks
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

SCRIPT = ("import json, os, pathlib\n"
          "run = pathlib.Path(os.environ['SUPERVISOR_RUN_DIR'])\n"
          "pathlib.Path('P.txt').write_text('done')\n"
          "(run / 'result.json').write_text(json.dumps({'status': 'COMPLETE', 'artifacts': ['P.txt'], 'notes': 'ok'}))\n")


def test_a_process_job_finds_its_run_folder_by_name(layout):
    make_task(layout, "P")
    tasks._update(layout, "P", lambda d: d.update(job={"adapter": "process", "argv": [sys.executable, "-c", SCRIPT]},
                                                  artifacts=["P.txt"], owned_paths=["P.txt"]))
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(20):
        sup.once()
        if tasks.load(layout, "P").status == TaskStatus.COMPLETE:
            break
    assert tasks.load(layout, "P").status == TaskStatus.COMPLETE
