import json, subprocess, sys, threading
import pytest
from supervisor._vendor import contract as c


def test_atomic_write_and_json_roundtrip(tmp_path):
    p = tmp_path / "a" / "b.json"
    c.atomic_json(p, {"x": 1})
    assert c.read_json(p) == {"x": 1}
    assert not list((tmp_path / "a").glob(".*.tmp"))


def test_atomic_write_concurrent_writers_leave_no_tmp(tmp_path):
    p = tmp_path / "f.txt"
    errors = []

    def w(i):
        try:
            for _ in range(100):
                c.atomic_write_text(p, f"writer {i}\n" * 50)
        except BaseException as exc:          # a thread's exception is only a warning to pytest: collect and fail
            errors.append(exc)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert errors == [], errors
    text = p.read_text()
    assert text.startswith("writer ") and len(set(text.splitlines())) == 1   # never torn
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_temp_names_are_unique_even_with_a_frozen_clock(tmp_path, monkeypatch):
    """Windows' clock is coarse enough for two writers to see the same time_ns(); temp names must not depend on it."""
    monkeypatch.setattr(c.time, "time_ns", lambda: 1)
    seen = []
    real_replace = c.Path.replace

    def spy(self, target):
        seen.append(self.name)
        return real_replace(self, target)
    monkeypatch.setattr(c.Path, "replace", spy)
    for i in range(5):
        c.atomic_write_text(tmp_path / "f.txt", str(i))
    assert len(seen) == 5 and len(set(seen)) == 5
    assert all(n.startswith(".f.txt.") and n.endswith(".tmp") for n in seen)
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_cleans_temp_when_the_write_itself_fails(tmp_path, monkeypatch):
    import os

    def boom(fd, *a, **k):
        os.close(fd)                      # Windows cannot unlink an open file; the real code closes via the handle
        raise OSError("disk full")
    monkeypatch.setattr(c.os, "fdopen", boom)
    with pytest.raises(OSError):
        c.atomic_write_text(tmp_path / "f.txt", "x")
    assert not list(tmp_path.glob(".*.tmp")) and not (tmp_path / "f.txt").exists()


def test_status_token():
    assert c.status_token("BLOCKED (reason)") == "BLOCKED"
    assert c.status_token("  **COMPLETE**") == "COMPLETE"


def test_manifest_digest_changed(tmp_path):
    (tmp_path / "d").mkdir(); (tmp_path / "d" / "x.txt").write_text("1")
    before = c.manifest([tmp_path / "d", tmp_path / "missing"])
    assert before[str(tmp_path / "missing")] == {"kind": "missing"}
    (tmp_path / "d" / "x.txt").write_text("2")
    after = c.manifest([tmp_path / "d", tmp_path / "missing"])
    assert c.changed_paths(before, after) == [str(tmp_path / "d" / "x.txt")]
    assert c.digest(before) != c.digest(after)


def test_manifest_rejects_links(tmp_path):
    (tmp_path / "t.txt").write_text("x")
    try:
        (tmp_path / "l").symlink_to(tmp_path / "t.txt")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink support")
    with pytest.raises(ValueError):
        c.manifest([tmp_path / "l"])


def test_contained_and_overlaps(tmp_path):
    assert c.contained(tmp_path / "a" / "b", [tmp_path / "a"])
    assert not c.contained(tmp_path / "c", [tmp_path / "a"])
    assert c.overlaps(tmp_path / "a", tmp_path / "a" / "b")
    assert not c.overlaps(tmp_path / "a", tmp_path / "b")


def test_result_object_exactly_one():
    good = 'notes\n```json\n{"kind":"delivery","schema_version":1,"status":"COMPLETE"}\n```'
    assert c.result_object(good, "delivery")["status"] == "COMPLETE"
    repeated = good + "\n" + good
    assert c.result_object(repeated, "delivery")["status"] == "COMPLETE"
    two = good + '\n```json\n{"kind":"delivery","schema_version":1,"status":"PARTIAL"}\n```'
    with pytest.raises(ValueError):
        c.result_object(two, "delivery")
    bare = 'answer {"kind":"delivery","schema_version":1,"status":"BLOCKED"} end'
    assert c.result_object(bare, "delivery")["status"] == "BLOCKED"
    with pytest.raises(ValueError):
        c.result_object("nothing here", "delivery")


def test_review_result_rules():
    ok = json.dumps({"kind": "review", "schema_version": 1, "verdict": "ACCEPT",
                     "findings": [{"category": "advisory", "blocking": False, "message": "m", "evidence": "e"}]})
    assert c.review_result(ok)["verdict"] == "ACCEPT"
    bad = json.dumps({"kind": "review", "schema_version": 1, "verdict": "ACCEPT",
                      "findings": [{"category": "correctness", "blocking": True, "message": "m", "evidence": "e"}]})
    with pytest.raises(ValueError):
        c.review_result(bad)
    with pytest.raises(ValueError):
        c.review_result(json.dumps({"kind": "review", "schema_version": 1, "verdict": "MAYBE", "findings": []}))


def test_structured_result_enforces_the_review_contract_only_for_review():
    """result_object() alone never checks a review's cross-field rules, so an adapter
    using it for both kinds let an ACCEPT with a blocking finding through. structured_result() is the one call
    both adapters now make."""
    delivery_ok = json.dumps({"kind": "delivery", "schema_version": 1, "status": "COMPLETE"})
    assert c.structured_result(delivery_ok, "delivery")["status"] == "COMPLETE"
    bad_review = json.dumps({"kind": "review", "schema_version": 1, "verdict": "ACCEPT",
                             "findings": [{"category": "correctness", "blocking": True, "message": "m", "evidence": "e"}]})
    with pytest.raises(ValueError):
        c.structured_result(bad_review, "review")
    good_review = json.dumps({"kind": "review", "schema_version": 1, "verdict": "REVISE",
                              "findings": [{"category": "correctness", "blocking": True, "message": "m", "evidence": "e"}]})
    assert c.structured_result(good_review, "review")["verdict"] == "REVISE"


def test_terminate_owned_kills_tree():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    c.terminate_owned(proc)
    assert proc.poll() is not None


def test_vendor_has_no_game_vocabulary():
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[1] / "supervisor" / "_vendor"
    for p in root.glob("*.py"):
        if p.name == "__init__.py":
            continue
        text = p.read_text(encoding="utf-8")
        body = text.split('"""', 2)[2]          # the module docstring may carry the attribution
        assert not re.search(r"(?i)towerdefense|sprint|epic|godot|astra", body), p


def test_package_has_no_source_project_history_references():
    """The package (including _vendor and the template) ships to strangers: no vocabulary, task ids, owner-task ids,
    runbooks or allowances from the project it was extracted from. `T<phase>-<nn>` is the documented id convention
    (docs/TASK_CONTRACT.md), so docs and tests may use `T0-01`-style examples; package code must not cite specific
    task or review ids as history ("T0-12.a1 failed ...")."""
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[1] / "supervisor"
    pattern = re.compile(r"(?i)towerdefense|sprint|epic|godot|astra|\bOT-\d|\$100|api_credits\.md|persona|studio|"
                         r"influencer|comfy|ollama|nudenet|\bT0-\d|review round|C:\\\\Users")
    hits = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix in (".pyc",) or "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in pattern.finditer(text):
            hits.append(f"{p.relative_to(root)}: {m.group(0)!r}")
    assert not hits, hits
