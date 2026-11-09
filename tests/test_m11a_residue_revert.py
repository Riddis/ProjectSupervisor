"""M11(a), first shape only (2.5.0): a write inside a verification window the attempt under verification did not
make. Of three shapes (the attempt's own cleanup of a previous attempt's edit of an unowned path; another attempt
running concurrently; a human edit outside Settings), this release builds the loop's revert for the first shape
only - its own recurrence ("two edits, two cleanups" in an earlier release): a rejected attempt's write outside its
owned paths stayed in the tree, so the RETRY's own before-manifest saw the residue, and the retry's innocent
restore of it was then charged as *its* own undeclared write - the exact defect this file reproduces failing, then
fixes.

The second shape (a concurrently dispatched attempt) is explicitly left out: a prior release ruled it out in
writing ("concurrent-attempt blame for undeclared writes — out ... a new mechanism and a larger release than this
one") and 2.5.0 does not reopen it - no regression is owed for a shape that stays unbuilt, and it stays named here
rather than silently dropped. The third shape (a human edit outside Settings) is not mechanically separable from
the first by this same mechanism, and `test_a_human_edit_of_an_unowned_path_is_also_reverted` below shows that the
revert does not need to tell them apart to be safe: it quarantines before it restores, so a wrongly reverted human
edit is recoverable, never silently lost."""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from supervisor import inbox, tasks, verify
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


def events(layout, name: str) -> list[dict]:
    """The structured log records of one event name, parsed - the Log row is about a record's own fields, so the
    test reads them as fields rather than looking for a substring in the file."""
    lines = (layout.state / "log.jsonl").read_text(encoding="utf-8").splitlines()
    return [r for r in (json.loads(ln) for ln in lines if ln.strip()) if r.get("event") == name]


def note_texts(layout, task_id: str) -> list[str]:
    t = tasks.load(layout, task_id)
    return [str(n.get("note") or "") for n in ((t.model_extra or {}).get("notes") or [])]


@pytest.fixture
def repo(layout):
    """A real git repository at the project root: the mechanism restores a reverted path's content from `git show
    HEAD:<path>`, gated on the recorded sha256 - the framework already assumes git in the project root (it
    checkpoints the store itself), so the test gives it one."""
    root = layout.root
    assert git(root, "init", "-q").returncode == 0
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "tests")
    git(root, "config", "commit.gpgsign", "false")
    (root / "other").mkdir(parents=True, exist_ok=True)
    (root / "other" / "shared.txt").write_text("original", encoding="utf-8")
    (root / ".gitignore").write_text("runs/\nstate/\n.venv/\n", encoding="utf-8")
    git(root, "add", "-A")
    assert git(root, "commit", "-q", "-m", "init").returncode == 0
    return root


@pytest.fixture
def scoped(layout, repo):
    make_task(layout, "T")

    def fn(doc):
        doc["owned_paths"] = ["owned"]
        doc["artifacts"] = ["owned/out.txt"]
        doc["job"] = {"adapter": "python", "fn": "noop"}

    tasks._update(layout, "T", fn)
    tasks.set_status(layout, "T", "IN_PROGRESS")
    (layout.root / "owned").mkdir(exist_ok=True)
    return tasks.load(layout, "T")


def test_a_rejected_attempts_own_residue_is_reverted_and_quarantined(layout, scoped, repo):
    """a1 writes an unowned path outside its scope; rejected for exactly that. The loop must revert the residue at
    rejection, quarantining a1's own content under its run directory (nothing is deleted)."""
    task = scoped
    run_dir1 = layout.runs / "T" / "T.a1"
    run_dir1.mkdir(parents=True)
    before1 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 1", encoding="utf-8")
    (layout.root / "other" / "shared.txt").write_text("a1 edit", encoding="utf-8")   # a1's own undeclared write
    outcome1 = verify.verify(layout, task, ["owned/out.txt"], before=before1)
    assert outcome1.ok is False and "other/shared.txt" in outcome1.detail
    assert outcome1.report.get("residue", {}).get("other/shared.txt")

    tasks.record_delivery(layout, "T", "dg1", actor="loop", paths=["owned/out.txt"])
    rec1 = {"task_id": "T", "attempt_id": "T.a1", "adapter": "python", "spec": {"run_dir": str(run_dir1)}}
    sup = Supervisor(layout, sleep_s=0)
    sup.record_outcome("T", "dg1", outcome1, rec1, substantive=False, worker_status="FAILED")

    # the tree is clean again - restored to what it was before a1 ever touched it
    assert (layout.root / "other" / "shared.txt").read_text(encoding="utf-8") == "original"
    # nothing is deleted: a1's own content is quarantined under its own run dir
    assert (run_dir1 / "undeclared" / "other" / "shared.txt").read_text(encoding="utf-8") == "a1 edit"

    # CONTRIBUTOR_RULING.md §5.2's Log row: one event per path with the TWO digests and the quarantine location -
    # the digest it was restored to and the one the attempt left behind (now readable in the quarantine), so the
    # record says what was replaced with what and not merely that something was.
    reverted = events(layout, "residue_reverted")
    assert len(reverted) == 1
    assert reverted[0]["path"] == "other/shared.txt"
    assert reverted[0]["before_sha256"] == hashlib.sha256(b"original").hexdigest()
    assert reverted[0]["after_sha256"] == hashlib.sha256(b"a1 edit").hexdigest()
    assert reverted[0]["quarantine"] == (run_dir1 / "undeclared" / "other" / "shared.txt").as_posix()


def test_a_reverted_path_is_named_in_one_note_on_the_task_and_opens_no_owner_item(layout, scoped, repo):
    """The other half of §5.2's Log row: the event alone is not enough. The next attempt reads the task, not
    `state/log.jsonl`, and a tree that changed under it between attempts is otherwise unexplained - so one note on
    the task names the reverted paths and where their previous content is quarantined. No owner item, though: an
    automatic repair that files an inbox item against itself is not a repair."""
    task = scoped
    run_dir1 = layout.runs / "T" / "T.a1"
    run_dir1.mkdir(parents=True)
    before1 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 1", encoding="utf-8")
    (layout.root / "other" / "shared.txt").write_text("a1 edit", encoding="utf-8")
    outcome1 = verify.verify(layout, task, ["owned/out.txt"], before=before1)
    assert outcome1.ok is False

    tasks.record_delivery(layout, "T", "dg1", actor="loop", paths=["owned/out.txt"])
    rec1 = {"task_id": "T", "attempt_id": "T.a1", "adapter": "python", "spec": {"run_dir": str(run_dir1)}}
    Supervisor(layout, sleep_s=0).record_outcome("T", "dg1", outcome1, rec1, substantive=False, worker_status="FAILED")

    texts = [n for n in note_texts(layout, "T") if "residue reverted" in n]
    assert len(texts) == 1, note_texts(layout, "T")
    assert "other/shared.txt" in texts[0]
    assert (run_dir1 / "undeclared").as_posix() in texts[0]
    assert inbox.open_items(layout, "owner") == []


def test_the_retrys_own_cleanup_of_already_reverted_residue_is_not_charged(layout, scoped, repo):
    """The exact recurrence: a2 is dispatched after a1's rejection and, unaware the loop already fixed it, 'cleans
    up' the unowned path itself (rewrites it back to the original value). Before the fix this is a
    genuine diff against a2's own before-snapshot (which still showed a1's residue) and a2 is wrongly rejected for
    an undeclared write. After the fix, a1's rejection already reverted the path, so a2's before-snapshot is already
    clean and its own identical rewrite is a no-op - no undeclared change, no false rejection."""
    task = scoped
    run_dir1 = layout.runs / "T" / "T.a1"
    run_dir1.mkdir(parents=True)
    before1 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 1", encoding="utf-8")
    (layout.root / "other" / "shared.txt").write_text("a1 edit", encoding="utf-8")
    outcome1 = verify.verify(layout, task, ["owned/out.txt"], before=before1)
    assert outcome1.ok is False

    tasks.record_delivery(layout, "T", "dg1", actor="loop", paths=["owned/out.txt"])
    rec1 = {"task_id": "T", "attempt_id": "T.a1", "adapter": "python", "spec": {"run_dir": str(run_dir1)}}
    sup = Supervisor(layout, sleep_s=0)
    sup.record_outcome("T", "dg1", outcome1, rec1, substantive=False, worker_status="FAILED")

    # a2 dispatches: its own before-manifest is taken AFTER a1's rejection-triggered revert
    before2 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 2", encoding="utf-8")
    (layout.root / "other" / "shared.txt").write_text("original", encoding="utf-8")   # a2's own "cleanup"
    outcome2 = verify.verify(layout, task, ["owned/out.txt"], before=before2)
    assert outcome2.ok is True, outcome2.detail


def test_a_human_edit_of_an_unowned_path_is_also_reverted(layout, scoped, repo):
    """§5.1: the revert cannot tell a human's edit of an unowned path from an agent's - and must not pretend to. A
    change to `other/shared.txt` present in a rejected attempt's own manifest is reverted and quarantined the same
    way whatever made it, which is what makes the mechanism safe for a human edit too: nothing is deleted, so a
    wrongly reverted edit is recoverable from the attempt's own run directory."""
    task = scoped
    run_dir1 = layout.runs / "T" / "T.a1"
    run_dir1.mkdir(parents=True)
    before1 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 1", encoding="utf-8")
    (layout.root / "other" / "shared.txt").write_text("a human edited this, not the agent", encoding="utf-8")
    outcome1 = verify.verify(layout, task, ["owned/out.txt"], before=before1)
    assert outcome1.ok is False

    tasks.record_delivery(layout, "T", "dg1", actor="loop", paths=["owned/out.txt"])
    rec1 = {"task_id": "T", "attempt_id": "T.a1", "adapter": "python", "spec": {"run_dir": str(run_dir1)}}
    Supervisor(layout, sleep_s=0).record_outcome("T", "dg1", outcome1, rec1, substantive=False, worker_status="FAILED")

    assert (layout.root / "other" / "shared.txt").read_text(encoding="utf-8") == "original"
    assert (run_dir1 / "undeclared" / "other" / "shared.txt").read_text(encoding="utf-8") == \
        "a human edited this, not the agent"


def test_a_path_git_cannot_account_for_is_left_exactly_as_found_and_named(layout, scoped, repo):
    """A before-digest git's own HEAD does not match (here: the path was never committed at all, so `git show`
    fails) is never guessed at - left exactly as found, and named on the task rather than silently dropped.
    CONTRIBUTOR_RULING.md §5.2's Limits row: this also opens one deduped owner decision item, because the next
    attempt will otherwise fail on something it cannot fix - a task note alone routes to nothing an owner reads."""
    task = scoped
    run_dir1 = layout.runs / "T" / "T.a1"
    run_dir1.mkdir(parents=True)
    (layout.root / "other" / "uncommitted.txt").write_text("already here, never committed", encoding="utf-8")
    before1 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 1", encoding="utf-8")
    (layout.root / "other" / "uncommitted.txt").write_text("the attempt's own edit", encoding="utf-8")
    outcome1 = verify.verify(layout, task, ["owned/out.txt"], before=before1)
    assert outcome1.ok is False

    tasks.record_delivery(layout, "T", "dg1", actor="loop", paths=["owned/out.txt"])
    rec1 = {"task_id": "T", "attempt_id": "T.a1", "adapter": "python", "spec": {"run_dir": str(run_dir1)}}
    Supervisor(layout, sleep_s=0).record_outcome("T", "dg1", outcome1, rec1, substantive=False, worker_status="FAILED")

    # left exactly as found: never reverted, never quarantined, never deleted
    assert (layout.root / "other" / "uncommitted.txt").read_text(encoding="utf-8") == "the attempt's own edit"
    assert not (run_dir1 / "undeclared" / "other" / "uncommitted.txt").exists()
    assert any("not reverted (a path git cannot account for: other/uncommitted.txt)" in n
              for n in note_texts(layout, "T")), note_texts(layout, "T")
    assert events(layout, "residue_reverted") == []      # no event for a path that was not reverted

    # one deduped owner decision item is opened for the path git cannot account for, and it carries the path under
    # its OWN reason: the item is what the Limits row exists to route, so the reason cannot live only in the note
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1
    item = items[0]
    assert item["task_id"] == "T" and item.get("residue_left") == ["other/uncommitted.txt"]
    assert "a path git cannot account for: other/uncommitted.txt" in item["summary"]

    # deduped: a second rejection that leaves exactly the same path untouched does not stack a second item
    Supervisor(layout, sleep_s=0)._notify_residue_left("T", {"a path git cannot account for":
                                                             ["other/uncommitted.txt"]})
    assert inbox.open_items(layout, "owner") == items


def test_each_cause_of_a_left_path_is_named_by_its_own_reason(layout, scoped, repo):
    """The prior round replaced one blended sentence ("git cannot account for them, or they are a link, a
    directory, too large, or hit a quarantine error") with one reason per cause, in both the task note and the
    owner item. Nothing pinned that mapping, so it could regress to a blend with every test still green: this
    drives all five causes through `_revert_residue()` and asserts each path appears under its own reason."""
    run_dir = layout.runs / "T" / "T.a1"
    (run_dir / "undeclared" / "other").mkdir(parents=True)
    # a file where the quarantine's own directory must be created: the mkdir below it raises OSError
    (run_dir / "undeclared" / "other" / "blocked").write_text("not a directory", encoding="utf-8")
    (layout.root / "other" / "blocked").mkdir()
    (layout.root / "other" / "blocked" / "x.txt").write_text("created by the attempt", encoding="utf-8")
    (layout.root / "other" / "uncommitted.txt").write_text("never committed", encoding="utf-8")
    digest = hashlib.sha256(b"never committed").hexdigest()
    residue = {
        "other/big.bin": {"before": {"kind": "large"}, "after": {"kind": "large"}},
        "other/adir": {"before": {"kind": "dir"}, "after": {"kind": "dir"}},
        "other/uncommitted.txt": {"before": {"kind": "file", "sha256": digest}, "after": {"kind": "file"}},
        "other/blocked/x.txt": {"before": {"kind": "missing"}, "after": {"kind": "file"}},
    }
    sup = Supervisor(layout, sleep_s=0)
    sup._revert_residue("T", residue, str(run_dir))
    # the fifth cause is the absence of a run directory to quarantine into: its own call, its own reason
    sup._revert_residue("T", {"other/no_run_dir.txt": {"before": {"kind": "file", "sha256": digest}}}, None)

    texts = " || ".join(note_texts(layout, "T"))
    for reason, rel in [("a file above LARGE_FILE_BYTES", "other/big.bin"),
                        ("a link or a directory", "other/adir"),
                        ("a path git cannot account for", "other/uncommitted.txt"),
                        ("a quarantine or restore error", "other/blocked/x.txt"),
                        ("no run directory to quarantine the current content into", "other/no_run_dir.txt")]:
        assert f"{reason}: {rel}" in texts, texts
    # nothing was restored or deleted, and the owner item says the same five things the note does
    assert (layout.root / "other" / "blocked" / "x.txt").read_text(encoding="utf-8") == "created by the attempt"
    assert events(layout, "residue_reverted") == []
    item = next(i for i in inbox.open_items(layout, "owner") if "other/adir" in (i.get("residue_left") or []))
    said = item["summary"] + (item.get("detail") or "")
    assert "a quarantine or restore error: other/blocked/x.txt" in said and \
           "a link or a directory: other/adir" in said


def test_a_created_paths_quarantine_mismatch_is_put_back_before_being_called_found(layout, scoped, repo, monkeypatch):
    """Round 5's own finding (chief_architect, round 4 review): for a path the attempt *created*,
    `path.replace(dest)` already moves it out of the tree before the post-move integrity re-read can raise, so a
    mismatch there used to leave the path gone - not "as found" - while the reason string still said `a
    quarantine or restore error` as if nothing had moved. The put-back now runs before that reason is used, so
    the path really is exactly as found, not merely described that way."""
    run_dir = layout.runs / "T" / "T.a1"
    (run_dir / "undeclared" / "other").mkdir(parents=True)
    (layout.root / "other" / "created.txt").write_text("created by the attempt", encoding="utf-8")
    residue = {"other/created.txt": {"before": {"kind": "missing"}, "after": {"kind": "file"}}}

    import pathlib
    real_read_bytes = pathlib.Path.read_bytes
    dest = run_dir / "undeclared" / "other" / "created.txt"

    def fake_read_bytes(self):
        if self == dest:
            return b"corrupted in transit"            # simulates the post-move copy not matching `current`
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", fake_read_bytes)

    sup = Supervisor(layout, sleep_s=0)
    sup._revert_residue("T", residue, str(run_dir))

    # put back exactly as it stood before the revert attempt - not gone, not the restore content (there is none)
    assert (layout.root / "other" / "created.txt").read_text(encoding="utf-8") == "created by the attempt"
    texts = " || ".join(note_texts(layout, "T"))
    assert "a quarantine or restore error: other/created.txt" in texts, texts
    assert events(layout, "residue_reverted") == []


def test_a_put_back_that_also_fails_says_the_path_is_not_known_found(layout, scoped, repo, monkeypatch):
    """The rarer double failure: the post-move mismatch above, *and* the put-back itself fails. The path is then
    genuinely gone, so the reason must say so rather than reuse "a quarantine or restore error" as if the path
    were untouched - the same silent mislabelling this round's other test closes, on the one path where it cannot
    be closed by a put-back."""
    run_dir = layout.runs / "T" / "T.a1"
    (run_dir / "undeclared" / "other").mkdir(parents=True)
    (layout.root / "other" / "created.txt").write_text("created by the attempt", encoding="utf-8")
    residue = {"other/created.txt": {"before": {"kind": "missing"}, "after": {"kind": "file"}}}

    import pathlib
    real_read_bytes = pathlib.Path.read_bytes
    real_write_bytes = pathlib.Path.write_bytes
    dest = run_dir / "undeclared" / "other" / "created.txt"
    path = layout.root / "other" / "created.txt"

    def fake_read_bytes(self):
        if self == dest:
            return b"corrupted in transit"
        return real_read_bytes(self)

    def fake_write_bytes(self, data):
        if self == path:
            raise OSError("disk full (simulated)")     # the put-back's own write fails too
        return real_write_bytes(self, data)

    monkeypatch.setattr(pathlib.Path, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(pathlib.Path, "write_bytes", fake_write_bytes)

    sup = Supervisor(layout, sleep_s=0)
    sup._revert_residue("T", residue, str(run_dir))

    assert not path.exists()                            # genuinely gone - never silently called "found"
    texts = " || ".join(note_texts(layout, "T"))
    assert "a quarantine or restore error, and the path could not be put back as it stood before the revert: " \
           "other/created.txt" in texts, texts
    assert events(layout, "residue_reverted") == []


def _two_tasks(layout):
    """Two scoped tasks, A and B, each owning its own root - the shape round 4's finding needs: A is rejected and
    reverted while B, a second writing task, has its own verification window already open."""
    for task_id, owned in (("A", "owned_a"), ("B", "owned_b")):
        make_task(layout, task_id)

        def fn(doc, owned=owned):
            doc["owned_paths"] = [owned]
            doc["artifacts"] = [f"{owned}/out.txt"]
            doc["job"] = {"adapter": "python", "fn": "noop"}

        tasks._update(layout, task_id, fn)
        tasks.set_status(layout, task_id, "IN_PROGRESS")
        (layout.root / owned).mkdir(exist_ok=True)
    return tasks.load(layout, "A"), tasks.load(layout, "B")


def test_a_concurrent_tasks_verification_is_not_charged_for_the_revert_of_a_modified_path(layout, repo):
    """Round 4's own finding: `_revert_residue()` writes into the tree at a path outside the rejected attempt's
    owned roots - exactly the paths a second, concurrently writing task's own verification also scans, and is not
    excused on (`ignore` only covers what another job *owns*, never a stranger's residue). A's edit of
    `other/shared.txt` is still in the tree when B's own before-snapshot is taken; A is then rejected and its
    revert restores the path to HEAD's content before B's own after-snapshot is taken. Without the revert recorded
    through `verify.record_own_write()`, B is charged for A's own repair - the fix re-creating the class it
    repairs. This is exactly the shape CONTRIBUTOR_RULING.md's ledger (`own_writes.jsonl` / `_supervisor_wrote`)
    exists to explain, so it must explain this write too, not only `settings.update`'s."""
    task_a, task_b = _two_tasks(layout)
    run_dir_a = layout.runs / "A" / "A.a1"
    run_dir_a.mkdir(parents=True)

    before_a = verify.snapshot(layout, task_a)
    (layout.root / "owned_a" / "out.txt").write_text("delivered a", encoding="utf-8")
    (layout.root / "other" / "shared.txt").write_text("a1 edit", encoding="utf-8")   # A's own undeclared write

    before_b = verify.snapshot(layout, task_b)   # B's window opens while A's dirty edit still sits in the tree

    outcome_a = verify.verify(layout, task_a, ["owned_a/out.txt"], before=before_a)
    assert outcome_a.ok is False and "other/shared.txt" in outcome_a.detail

    tasks.record_delivery(layout, "A", "dgA", actor="loop", paths=["owned_a/out.txt"])
    rec_a = {"task_id": "A", "attempt_id": "A.a1", "adapter": "python", "spec": {"run_dir": str(run_dir_a)}}
    Supervisor(layout, sleep_s=0).record_outcome("A", "dgA", outcome_a, rec_a, substantive=False, worker_status="FAILED")

    # A's rejection reverted the path inside B's still-open window
    assert (layout.root / "other" / "shared.txt").read_text(encoding="utf-8") == "original"

    (layout.root / "owned_b" / "out.txt").write_text("delivered b", encoding="utf-8")
    outcome_b = verify.verify(layout, task_b, ["owned_b/out.txt"], before=before_b)
    assert outcome_b.ok is True, outcome_b.detail


def test_a_concurrent_tasks_verification_is_not_charged_for_the_revert_of_a_created_path(layout, repo):
    """The ledger edge the previous round's `record_own_write`/`_supervisor_wrote` could not express at all: A
    creates a brand-new unowned path, B's own before-snapshot sees it already there, A is rejected and the revert
    DELETES it (restoring A's own absence) before B's own after-snapshot is taken. `_supervisor_wrote()` must
    accept a target of `None` (the ledger's own `"after": None`) for a deletion to be explained the same way a
    replacement is."""
    task_a, task_b = _two_tasks(layout)
    run_dir_a = layout.runs / "A" / "A.a1"
    run_dir_a.mkdir(parents=True)

    before_a = verify.snapshot(layout, task_a)
    (layout.root / "owned_a" / "out.txt").write_text("delivered a", encoding="utf-8")
    (layout.root / "other" / "created.txt").write_text("a1 created", encoding="utf-8")   # A's own undeclared write

    before_b = verify.snapshot(layout, task_b)   # B's before-manifest already holds A's newly created path

    outcome_a = verify.verify(layout, task_a, ["owned_a/out.txt"], before=before_a)
    assert outcome_a.ok is False and "other/created.txt" in outcome_a.detail

    tasks.record_delivery(layout, "A", "dgA", actor="loop", paths=["owned_a/out.txt"])
    rec_a = {"task_id": "A", "attempt_id": "A.a1", "adapter": "python", "spec": {"run_dir": str(run_dir_a)}}
    Supervisor(layout, sleep_s=0).record_outcome("A", "dgA", outcome_a, rec_a, substantive=False, worker_status="FAILED")

    # A's rejection reverted the created path out of existence, inside B's still-open window
    assert not (layout.root / "other" / "created.txt").exists()

    (layout.root / "owned_b" / "out.txt").write_text("delivered b", encoding="utf-8")
    outcome_b = verify.verify(layout, task_b, ["owned_b/out.txt"], before=before_b)
    assert outcome_b.ok is True, outcome_b.detail
