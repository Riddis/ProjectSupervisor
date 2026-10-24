import hashlib
import json

import yaml

from supervisor import packets, tasks
from supervisor._vendor.contract import digest as digest_of
from tests.conftest import make_task


def _digest(entries: dict) -> str:
    """contract.digest() spelled out the way review_brief() documents it, so the rule is checked and not borrowed."""
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_frozen_inputs_is_a_sha256_manifest_over_existing_paths(layout):
    (layout.root / "out.txt").write_text("hello")
    m = packets.frozen_inputs(layout, ["out.txt", "missing.txt"])
    assert set(m) == {"out.txt"} and m["out.txt"]["kind"] == "file" and len(m["out.txt"]["sha256"]) == 64


def test_frozen_inputs_changes_when_the_file_does(layout):
    (layout.root / "out.txt").write_text("v1")
    m1 = packets.frozen_inputs(layout, ["out.txt"])
    (layout.root / "out.txt").write_text("v2")
    m2 = packets.frozen_inputs(layout, ["out.txt"])
    assert m1["out.txt"]["sha256"] != m2["out.txt"]["sha256"]


def test_role_card_reads_the_configured_card(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    layout.agents.mkdir(parents=True, exist_ok=True)
    (layout.agents / "engineer.md").write_text("# Engineer\n\nBuild things.")
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"roles": {"engineer": {"card": "engineer.md"}}}))
    assert "Build things" in packets.role_card(layout, "engineer")
    assert packets.role_card(layout, "nobody") == ""


def test_delivery_brief_contains_the_contract_and_result_block(layout):
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    brief = packets.delivery_brief(layout, task)
    assert task.objective in brief and task.acceptance in brief and '"kind":"delivery"' in brief


def test_review_brief_carries_the_digest_and_artifacts_never_the_producers_notes(layout):
    make_task(layout, "T")
    def fn(doc):
        doc["result"] = "I, the producer, think this is great."
    tasks._update(layout, "T", fn)
    task = tasks.load(layout, "T")
    brief = packets.review_brief(layout, task, "d" * 64, ["out/thing.py"], "code_review")
    assert "d" * 64 in brief and "out/thing.py" in brief and task.acceptance in brief
    assert "I, the producer" not in brief


def test_review_brief_documents_the_manifest_rule(layout):
    """a reviewer that independently hashes the listed artifacts to check the frozen-input
    digest needs the exact rule to reproduce it, or a mismatch of its own making gets filed as a defect in the
    delivery."""
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    brief = packets.review_brief(layout, task, "d" * 64, ["out/thing.py"], "code_review")
    assert "sha256" in brief and "root-relative POSIX" in brief
    assert '"kind": "file"' in brief


def test_review_brief_manifest_rule_is_reproducible_from_the_brief_text_alone(layout):
    """the documented rule must actually reproduce the frozen digest for the exact path list the
    brief hands over — including the "kind" key contract.manifest() writes on every entry
    (supervisor/_vendor/contract.py) and
    including a listed-but-absent artifact, which frozen_inputs() drops rather than recording as
    {"kind": "missing"}. The list the
    reviewer rehashes is the same list review.dispatch() freezes (supervisor/review.py), so it can contain an
    artifact the producer claimed but never wrote."""
    (layout.root / "a.py").write_text("print('a')")
    (layout.root / "b.py").write_text("print('b')")
    paths = ["a.py", "b.py", "missing.py"]
    manifest = packets.frozen_inputs(layout, paths)
    real_digest = digest_of(manifest)
    make_task(layout, "T-manifest-rule")
    task = tasks.load(layout, "T-manifest-rule")
    brief = packets.review_brief(layout, task, real_digest, paths, "code_review")

    # The brief must state the rule this test then follows: file entries carry "kind", and a listed path that is
    # not on disk is left out instead of being recorded as missing.
    assert real_digest in brief and "missing.py" in brief
    assert '"kind": "file"' in brief
    assert "does not exist when the manifest is taken is left out of it entirely" in brief
    assert '"kind": "missing"' not in brief

    # Rebuild the manifest from nothing but that rule: root-relative POSIX path keys, each file on disk as
    # {"kind": "file", "size", "sha256"}, absent paths omitted, dumped sorted with no extra whitespace, hashed.
    reconstructed = {}
    for p in paths:
        full = layout.root / p
        if not full.exists():
            continue
        data = full.read_bytes()
        reconstructed[full.relative_to(layout.root).as_posix()] = {
            "kind": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }
    assert reconstructed == manifest and "missing.py" not in reconstructed
    assert _digest(reconstructed) == real_digest

    # And the rule the old wording described — a {"kind": "missing"} entry for the absent artifact — reproduces a
    # different digest, which is exactly the mismatch a reviewer would have filed as a defect in the delivery.
    assert _digest({**reconstructed, "missing.py": {"kind": "missing"}}) != real_digest


def test_review_brief_hands_over_the_supervisors_verification_report(layout):
    """a read-only review lane has no Bash and no writable scratch directory, so it cannot
    re-run the declared tests itself; the brief hands it the supervisor's own verification record instead of
    leaving it to discover mid-review that it cannot execute anything."""
    make_task(layout, "T")
    tasks.record_delivery(layout, "T", "d" * 64, actor="test", paths=["out/thing.py"])
    report_path = layout.runs / "T" / "T.a1" / "verification.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text('{"ok": true, "detail": "artifacts present; declared tests passed", '
                           '"tests": [{"command": "python -m pytest tests/test_x.py", "exit_code": 0}]}', encoding="utf-8")
    tasks.record_verification(layout, "T", "d" * 64, True, report="runs/T/T.a1/verification.json",
                              detail="artifacts present; declared tests passed")
    task = tasks.load(layout, "T")
    brief = packets.review_brief(layout, task, "d" * 64, ["out/thing.py"], "code_review")
    assert "Verification already performed" in brief
    assert "python -m pytest tests/test_x.py" in brief and "exit 0" in brief


def test_review_brief_omits_verification_section_when_digest_does_not_match(layout):
    make_task(layout, "T")
    tasks.record_delivery(layout, "T", "d" * 64, actor="test", paths=["out/thing.py"])
    tasks.record_verification(layout, "T", "d" * 64, True, detail="ok")
    task = tasks.load(layout, "T")
    brief = packets.review_brief(layout, task, "e" * 64, ["out/thing.py"], "code_review")   # a different digest
    assert "Verification already performed" not in brief


def test_revision_brief_quotes_the_failure_verbatim(layout):
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    prior = packets.delivery_brief(layout, task)
    brief = packets.revision_brief(task, prior, "verification", "missing artifacts: out.txt")
    assert "missing artifacts: out.txt" in brief and "Revision required" in brief and prior in brief


def test_review_brief_tells_the_reviewer_the_finding_category_vocabulary(layout):
    """The contract refuses an unknown category; a reviewer that was never told the names loses its verdict over a
    spelling. The brief states them, in the contract's own words."""
    from supervisor._vendor.contract import FINDING_CATEGORIES
    make_task(layout, "T")
    brief = packets.review_brief(layout, tasks.load(layout, "T"), "d" * 64, ["out/thing.py"], "code_review")
    assert packets.FINDING_CATEGORY_LIST in brief
    assert set(packets.FINDING_CATEGORY_LIST.split(" | ")) == FINDING_CATEGORIES
