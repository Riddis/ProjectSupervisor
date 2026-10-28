"""1.13.0, a skill-trial finding (task-author): a task whose `owned_paths` names a folder that does not exist yet
failed its first delivery with "undeclared changes inside owned paths: <folder>", and spent a revision. Before the
job the delivery manifest records the folder as missing, after it as a directory, and that change was counted as
an undeclared write, although the only real change was the declared file inside. A folder appearing (or going away)
is not a delivery: only file changes are judged. A file replaced by something else still is."""
from __future__ import annotations

from supervisor import tasks
from supervisor.adapters import python as pyad
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from supervisor.verify import _undeclared
from tests.test_loop import ptask


@pyad.job("write_in_new_folder")
def _write_in_new_folder(root, run_dir, name="out.txt", **_):
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text("x")
    return {"status": "COMPLETE", "artifacts": [name], "notes": "wrote " + name}


def test_a_new_owned_folder_does_not_fail_the_first_delivery(layout):
    assert not (layout.root / "digest").exists()
    ptask(layout, "D", name="digest/week.txt", owned=("digest",))
    tasks._update(layout, "D", lambda d: d["job"].update(fn="write_in_new_folder"))
    Supervisor(layout, sleep_s=0).once()
    t = tasks.load(layout, "D")
    assert t.status == TaskStatus.COMPLETE, (t.status, t.result)
    assert len(t.attempts) == 1


def test_only_file_changes_count(tmp_path):
    before = {"digest": {"kind": "missing"}, "keep.txt": {"kind": "file", "sha256": "a"}}
    after = {"digest": {"kind": "directory"}, "digest/week.txt": {"kind": "file", "sha256": "b"},
             "keep.txt": {"kind": "directory"}}
    inside, _ = _undeclared(before, after, {"digest/week.txt"}, ["digest", "keep.txt"], tmp_path, ())
    assert inside == ["keep.txt"]                            # a file that became a folder is still a change
