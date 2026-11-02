"""The owner's Settings edits are the supervisor's writes, not the running delivery's (2.1.1). The owner changed
config/routing.yaml in the Control Center while a delivery ran; the delivery's verification counted the edit as its
undeclared write outside owned paths, the retry was told so and reverted the owner's values. A change the recorded
supervisor writes alone account for is no delivery's; an agent's own edit of the same file still fails."""
from __future__ import annotations

import json

import pytest

from supervisor import settings, tasks, verify
from tests.conftest import make_task

ROUTING = "limits:\n  max_agent_jobs: 2          # owner\n  max_resource_jobs: 1\n"


@pytest.fixture
def routing(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    path = layout.config / "routing.yaml"
    path.write_bytes(ROUTING.encode("utf-8"))
    return path


@pytest.fixture
def scoped(layout, routing):
    make_task(layout, "T")
    def fn(doc):
        doc["owned_paths"] = ["owned"]
        doc["artifacts"] = ["owned/out.txt"]
    tasks._update(layout, "T", fn)
    (layout.root / "owned").mkdir()
    return tasks.load(layout, "T")


def _deliver(layout):
    (layout.root / "owned" / "out.txt").write_text("ok")


def test_an_owner_settings_edit_during_a_delivery_is_not_its_undeclared_write(layout, scoped, routing):
    before = verify.snapshot(layout, scoped)
    settings.update(layout, "limits.max_agent_jobs", "4")
    _deliver(layout)
    outcome = verify.verify(layout, scoped, ["owned/out.txt"], before=before)
    assert outcome.ok is True, outcome.detail
    assert b"max_agent_jobs: 4" in routing.read_bytes()


def test_several_owner_edits_chain(layout, scoped, routing):
    before = verify.snapshot(layout, scoped)
    settings.update(layout, "limits.max_agent_jobs", "10")
    settings.update(layout, "limits.max_agent_jobs", "4")
    settings.update(layout, "limits.max_resource_jobs", "2")
    _deliver(layout)
    assert verify.verify(layout, scoped, ["owned/out.txt"], before=before).ok is True


def test_an_owner_edit_is_explained_in_exclusive_mode_too(layout, routing):
    make_task(layout, "X")
    task = tasks.load(layout, "X")
    assert not task.scoped_paths()
    before = verify.snapshot(layout, task)
    settings.update(layout, "limits.max_agent_jobs", "4")
    assert verify.verify(layout, task, [], before=before).ok is True


def test_an_agents_own_edit_of_the_settings_file_still_fails(layout, scoped, routing):
    before = verify.snapshot(layout, scoped)
    routing.write_bytes(ROUTING.replace("max_agent_jobs: 2", "max_agent_jobs: 9").encode())
    _deliver(layout)
    outcome = verify.verify(layout, scoped, ["owned/out.txt"], before=before)
    assert outcome.ok is False and "config/routing.yaml" in outcome.detail


def test_an_agent_edit_under_a_later_owner_edit_still_fails(layout, scoped, routing):
    """The owner's write starts from content an agent wrote: no chain of supervisor writes leads from the snapshot to
    what the file holds now, so the agent's part is not hidden behind the owner's."""
    before = verify.snapshot(layout, scoped)
    routing.write_bytes((ROUTING + "# agent\n").encode())
    settings.update(layout, "limits.max_agent_jobs", "4")
    _deliver(layout)
    outcome = verify.verify(layout, scoped, ["owned/out.txt"], before=before)
    assert outcome.ok is False and "config/routing.yaml" in outcome.detail


def test_an_agent_reverting_an_owner_edit_still_fails(layout, scoped, routing):
    """The cascade the owner saw: a retry put the old values back. That is the agent's write."""
    settings.update(layout, "limits.max_agent_jobs", "4")
    before = verify.snapshot(layout, scoped)
    routing.write_bytes(ROUTING.encode())
    _deliver(layout)
    outcome = verify.verify(layout, scoped, ["owned/out.txt"], before=before)
    assert outcome.ok is False and "config/routing.yaml" in outcome.detail


def test_the_write_is_recorded_before_the_file_changes(layout, routing, monkeypatch):
    from supervisor import store
    def boom(path, text):
        raise OSError("disk full")
    monkeypatch.setattr(store, "rewrite_text", boom)
    with pytest.raises(OSError):
        settings.update(layout, "limits.max_agent_jobs", "4")
    lines = (layout.runtime / verify.OWN_WRITES).read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    assert entry["path"] == "config/routing.yaml" and entry["why"] == "settings limits.max_agent_jobs"
    assert routing.read_bytes() == ROUTING.encode()


def test_an_unchanged_value_records_nothing_and_a_bad_line_explains_nothing(layout, routing):
    settings.update(layout, "limits.max_agent_jobs", "2")
    assert not (layout.runtime / verify.OWN_WRITES).exists()
    (layout.runtime / verify.OWN_WRITES).parent.mkdir(parents=True, exist_ok=True)
    (layout.runtime / verify.OWN_WRITES).write_text("not json\n{\"path\": 1}\n", encoding="utf-8")
    assert verify.own_transitions(layout).get("config/routing.yaml") is None


def test_crlf_files_are_hashed_as_written(layout, scoped, routing):
    routing.write_bytes(ROUTING.replace("\n", "\r\n").encode())
    before = verify.snapshot(layout, scoped)
    settings.update(layout, "limits.max_agent_jobs", "4")
    assert b"\r\n" in routing.read_bytes()
    _deliver(layout)
    assert verify.verify(layout, scoped, ["owned/out.txt"], before=before).ok is True
