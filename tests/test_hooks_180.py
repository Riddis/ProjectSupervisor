"""Framework 1.8.0: the extension hooks a project with its own heavy resource, review pages and content rules
needs — resource lock by cost category, extension command kinds, extension-owned inbox kinds, job-aware packet
screens, extension templates and static files, the shared GPU probe — and the fixes that ride along."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from supervisor import commands, doctor, extensions, gate, gpu, inbox, jobs, policy, project, scheduler, tasks
from supervisor.adapters import REGISTRY
from supervisor.loop import Supervisor
from supervisor.paths import Layout
from supervisor.ui.app import create_app
from supervisor.ui.auth import CSRF_COOKIE
from tests.conftest import make_task

CSRF = "test-csrf-value"


def _configure(root: Path, entries: list, modules: dict[str, str], files: dict[str, str] | None = None) -> Layout:
    (root / "extensions").mkdir(parents=True, exist_ok=True)
    for name, code in modules.items():
        (root / "extensions" / f"{name}.py").write_text(code, encoding="utf-8")
    for rel, text in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    doc = dict(project.load(Layout(root)))
    doc["name"] = doc.get("name") or "proj"
    doc["extensions"] = entries
    project.write(Layout(root), doc)
    extensions.reset()
    return Layout(root).ensure()


def _routing(layout: Layout, doc: dict) -> None:
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def _client(layout: Layout) -> TestClient:
    c = TestClient(create_app(layout), base_url="http://127.0.0.1")
    c.cookies.set(CSRF_COOKIE, CSRF)
    return c


# ------------------------------------------------------------------ hook 1: limits.resource_cost_categories

def _local_task(layout, tid):
    make_task(layout, tid)
    def fn(doc):
        doc["cost_category"] = "local"
        doc["owned_paths"] = [f"out/{tid}"]
    tasks._update(layout, tid, fn)


def test_a_delivery_of_a_listed_cost_category_holds_the_resource_slot_on_a_model_lane(layout):
    _routing(layout, {"version": 1, "cost_lanes": {"local": "claude_cli"},
                      "limits": {"max_agent_jobs": 4, "max_resource_jobs": 1, "resource_cost_categories": ["local"]}})
    _local_task(layout, "L1")
    _local_task(layout, "L2")
    assert gate.holds_resource(layout, tasks.load(layout, "L1"), "claude_cli")
    assert not gate.holds_resource(layout, tasks.load(layout, "L1"), "claude_cli", kind="review")
    picks = scheduler.plan(layout)
    assert [(t.id, name) for t, name in picks] == [(tasks.load(layout, "L1").id, "claude_cli")]   # one slot, one pick
    running = [{"task_id": "L1", "adapter": "claude_cli", "phase": "RUNNING", "gpu": True}]
    reason = gate.check(layout, tasks.load(layout, "L2"), "claude_cli", "delivery", running, decision="RUN")
    assert reason and "resource-lock limit" in reason


def test_without_the_setting_a_model_lane_delivery_never_holds_the_slot(layout):
    _routing(layout, {"version": 1, "cost_lanes": {"local": "claude_cli"}, "limits": {"max_agent_jobs": 4}})
    _local_task(layout, "L1")
    _local_task(layout, "L2")
    assert not gate.holds_resource(layout, tasks.load(layout, "L1"), "claude_cli")
    assert len(scheduler.plan(layout)) == 2
    assert gate.limits(layout)["resource_cost_categories"] == []


def _no_launch(monkeypatch):
    """dispatch() prepares, records and launches; the suite never starts a real model client, so submit is faked."""
    for lane in ("claude_cli", "codex_cli"):
        monkeypatch.setattr(REGISTRY[lane], "submit", lambda spec: {"kind": "fake", "pid": None})
        monkeypatch.setattr(REGISTRY[lane], "alive", lambda handle: True)


def test_the_job_record_gpu_flag_follows_the_same_rule_so_a_restart_readopts_the_lock(layout, monkeypatch):
    _no_launch(monkeypatch)
    _routing(layout, {"version": 1, "cost_lanes": {"local": "claude_cli"},
                      "limits": {"resource_cost_categories": ["local"]}})
    _local_task(layout, "L1")
    sup = Supervisor(layout, sleep_s=0)
    rec = sup.dispatch(tasks.load(layout, "L1"), "claude_cli")
    assert jobs.read(layout, rec["attempt_id"])["gpu"] is True
    make_task(layout, "C1")                                               # an ordinary claude task: no slot
    rec2 = sup.dispatch(tasks.load(layout, "C1"), "claude_cli")
    assert jobs.read(layout, rec2["attempt_id"])["gpu"] is False


# ------------------------------------------------------------------ hook 2: registry.command

COMMAND_EXT = '''
def setup(registry):
    def bump(layout, payload, actor):
        (layout.root / "bumped.txt").write_text(f"{payload['_id']} {actor}", encoding="utf-8")
        return {"bumped": True}
    registry.command("counter.bump", bump)
'''


def test_an_extension_command_kind_is_applied_like_a_builtin(layout):
    _configure(layout.root, ["counter"], {"counter": COMMAND_EXT})
    reg = extensions.load(layout)
    assert "counter.bump" in reg.commands and "counter.bump" in commands.HANDLERS
    out = commands.submit_or_apply(layout, "counter.bump", {}, actor="owner-ui")
    assert out["ok"] and out["result"] == {"bumped": True}
    assert (layout.root / "bumped.txt").read_text(encoding="utf-8").endswith(" owner-ui")


def test_an_extension_cannot_hijack_a_builtin_command_kind(layout):
    code = COMMAND_EXT.replace('"counter.bump"', '"inbox.answer"')
    _configure(layout.root, ["hijack"], {"hijack": code})
    reg = extensions.load(layout)
    assert reg.loaded == [] and "already registered" in reg.errors[0]
    assert commands.HANDLERS["inbox.answer"].__name__ == "_inbox_answer"


# ------------------------------------------------------------------ hook 3: registry.inbox_kind

ROUNDS_EXT = '''
from fastapi import APIRouter
from supervisor import inbox
router = APIRouter()

@router.get("/grid/{item_id}")
def grid(item_id: str):
    return {"item": item_id}

def setup(registry):
    registry.inbox_kind("labelling", "Labelling sessions", page="/grid/{id}", answerable=False,
                        show_evidence=False, score_keys=("identity_mean", "hands"))
    registry.inbox_kind("spotcheck", "Spot checks", page="/grid/{id}", answerable=True)
    registry.ui_router(router, "Grid", "/grid")
    def finish(layout, payload, actor):
        return inbox.answer(layout, payload["item_id"], "ACCEPT", by=actor, command_id=payload["_id"], internal=True)
    registry.command("grid.submit", finish)
'''


def _labelling_item(layout):
    return inbox.create(layout, for_role="owner", subject="label 2 images", summary="blind", kind="labelling",
                        extra={"items": [{"image": "a.png", "identity_mean": 0.91, "hands": 0.4},
                                         {"image": "b.png", "identity_mean": 0.12, "hands": 0.9}]})


def test_an_extension_owned_kind_is_closed_only_by_the_extension(layout):
    _configure(layout.root, ["rounds"], {"rounds": ROUNDS_EXT})
    item = _labelling_item(layout)
    with pytest.raises(inbox.InboxError, match="finalised by its extension.*grid"):
        inbox.answer(layout, item["id"], "ACCEPT")
    out = commands.submit_or_apply(layout, "inbox.answer", {"item_id": item["id"], "response": "ACCEPT"}, "owner")
    assert not out["ok"] and "finalised by its extension" in out["error"]
    assert inbox.load(layout, item["id"])["status"] == "open"
    out = commands.submit_or_apply(layout, "grid.submit", {"item_id": item["id"]}, "owner-ui")
    assert out["ok"] and inbox.load(layout, item["id"])["status"] == "answered"


def test_the_inbox_page_hides_blind_scores_links_the_page_and_drops_the_form(layout):
    _configure(layout.root, ["rounds"], {"rounds": ROUNDS_EXT})
    item = _labelling_item(layout)
    spot = inbox.create(layout, for_role="owner", subject="spot check", summary="look", kind="spotcheck",
                        extra={"items": [{"image": "c.png", "score": 3}]})
    client = _client(layout)
    html = client.get("/inbox").text
    assert "Labelling sessions" in html and "Spot checks" in html
    assert f"/grid/{item['id']}" in html and f"/grid/{spot['id']}" in html
    assert "0.91" not in html and "identity_mean" not in html             # blind: scores never reach the card
    assert "2 item(s)" in html
    assert f'action="/inbox/{item["id"]}/answer"' not in html             # not answerable from the generic form
    assert f'action="/inbox/{spot["id"]}/answer"' in html                 # answerable kinds keep the form
    assert "<td>3</td>" in html                                           # and show their evidence
    r = client.post(f"/inbox/{item['id']}/answer", data={"csrf_token": CSRF, "response": "ACCEPT"}, follow_redirects=False)
    assert r.status_code == 400 and "finalised by its extension" in r.text
    assert client.get(f"/grid/{item['id']}").json() == {"item": item["id"]}


def test_builtin_kinds_cannot_be_claimed_and_a_kind_is_claimed_once(layout):
    code = ROUNDS_EXT.replace('"labelling"', '"approval"')
    _configure(layout.root, ["rounds"], {"rounds": code})
    reg = extensions.load(layout)
    assert reg.loaded == [] and "built in" in reg.errors[0]
    _configure(layout.root, ["rounds"], {"rounds": ROUNDS_EXT.replace('"spotcheck"', '"labelling"')})
    reg = extensions.load(layout)
    assert reg.loaded == [] and "already claimed" in reg.errors[0]


# ------------------------------------------------------------------ hook 4: job-aware packet screens

CONTENT_EXT = '''
from supervisor.policy import PolicyViolation
def setup(registry):
    def by_job(layout, text, where, *, job=None, lane=None):
        if job and job.get("channel") == "members_only":
            raise PolicyViolation(f"{where} refused: channel members_only is local-only (lane {lane})")
    def by_text(layout, text, where):
        if "CUSTOMER-RECORD" in text:
            raise PolicyViolation(f"{where} refused: customer record")
    registry.packet_screen(by_job)
    registry.packet_screen(by_text)
'''


def test_a_screen_may_refuse_on_the_job_and_old_text_screens_keep_working(layout):
    _configure(layout.root, ["content"], {"content": CONTENT_EXT})
    policy.assert_packet_clean("fine", layout, "p", job={"channel": "public"}, lane="claude_cli")
    with pytest.raises(policy.PolicyViolation, match="members_only.*claude_cli"):
        policy.assert_packet_clean("fine", layout, "p", job={"channel": "members_only"}, lane="claude_cli")
    with pytest.raises(policy.PolicyViolation, match="customer record"):
        policy.assert_packet_clean("has CUSTOMER-RECORD", layout, "p")
    policy.assert_packet_clean("fine", layout, "p")                     # no context at all: text screens only


def test_hosted_adapters_pass_the_job_to_the_screens(layout, monkeypatch):
    _no_launch(monkeypatch)
    _configure(layout.root, ["content"], {"content": CONTENT_EXT})
    make_task(layout, "H")
    def fn(doc):
        doc["job"] = {"channel": "members_only", "role": "engineer"}
    tasks._update(layout, "H", fn)
    for lane in ("claude_cli", "codex_cli", "claude_api"):
        with pytest.raises(policy.PolicyViolation, match=f"members_only.*{lane}"):
            REGISTRY[lane].prepare(layout, tasks.load(layout, "H"), f"H.{lane}")
    sup = Supervisor(layout, sleep_s=0)                                  # through the loop: a visible FAILED attempt
    rec = sup.dispatch(tasks.load(layout, "H"), "claude_cli")
    assert "members_only" in (rec.get("error") or "") and rec["phase"] != "RUNNING"


def test_a_screen_raising_type_error_inside_is_not_mistaken_for_a_signature_mismatch(layout):
    code = '''
def setup(registry):
    def broken(layout, text, where, *, job=None):
        raise TypeError("the screen itself is broken")
    registry.packet_screen(broken)
'''
    _configure(layout.root, ["broken"], {"broken": code})
    with pytest.raises(TypeError, match="itself is broken"):
        policy.assert_packet_clean("x", layout, "p", job={})


# ------------------------------------------------------------------ hooks 5 and 6: templates and static files

PAGE_EXT = '''
from pathlib import Path
from fastapi import APIRouter, Depends, Request
from supervisor.ui.auth import require_auth
from supervisor.ui.deps import render
router = APIRouter()
HERE = Path(__file__).resolve().parent

@router.get("/gallery")
def gallery(request: Request, layout=Depends(require_auth)):
    return render(request, "gallery.html", {"count": 3})

def setup(registry):
    registry.template_dir(HERE / "templates")
    registry.static_dir(HERE / "static", "gallery")
    registry.ui_router(router, "Gallery", "/gallery")
'''
GALLERY_HTML = '''{% extends "base.html" %}
{% block title %}Gallery — Control Center{% endblock %}
{% block content %}<section class="panel" id="gallery-panel">{{ count }} pictures</section>
<script src="/static/gallery/gallery.js"></script>{% endblock %}
'''


def test_an_extension_page_extends_the_framework_base_and_serves_its_own_assets(layout):
    _configure(layout.root, ["pages"], {"pages": PAGE_EXT},
               files={"extensions/templates/gallery.html": GALLERY_HTML,
                      "extensions/templates/base.html": "HIJACKED",
                      "extensions/static/gallery.js": "console.log('gallery');"})
    client = _client(layout)
    html = client.get("/gallery").text
    assert "3 pictures" in html and 'id="gallery-panel"' in html
    assert "HIJACKED" not in html and "<nav" in html                     # the framework's base.html, not the extension's
    assert 'href="/gallery"' in html                                     # sidebar entry from ui_router
    r = client.get("/static/gallery/gallery.js")
    assert r.status_code == 200 and "gallery" in r.text
    assert client.get("/static/live.js").status_code == 200              # the framework's own static files still serve
    assert client.get("/static/gallery/missing.js").status_code == 404


def test_template_and_static_registrations_are_validated(layout):
    bad = PAGE_EXT.replace('registry.static_dir(HERE / "static", "gallery")', 'registry.static_dir(HERE / "static", "a/b")')
    _configure(layout.root, ["pages"], {"pages": bad}, files={"extensions/templates/x.html": "", "extensions/static/x.js": ""})
    reg = extensions.load(layout)
    assert reg.loaded == [] and "one path segment" in reg.errors[0]
    bad = PAGE_EXT.replace('HERE / "templates"', 'HERE / "nowhere"')
    _configure(layout.root, ["pages"], {"pages": bad}, files={"extensions/static/x.js": ""})
    reg = extensions.load(layout)
    assert reg.loaded == [] and "not a directory" in reg.errors[0]


# ------------------------------------------------------------------ hook 7: supervisor.gpu

def test_gpu_probe_reads_nvidia_smi_and_never_raises(monkeypatch):
    class Out:
        returncode = 0
        stdout = "8192\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Out())
    assert gpu.total_mb() == 8192 and gpu.available()

    def boom(*a, **k):
        raise FileNotFoundError("nvidia-smi")
    monkeypatch.setattr(subprocess, "run", boom)
    assert gpu.free_mb() is None and gpu.used_mb() is None and not gpu.available()


# ------------------------------------------------------------------ fixes

def test_a_failed_optional_extension_is_logged_not_only_shown_by_doctor(layout, capsys):
    _configure(layout.root, [{"name": "opt", "required": False}], {"opt": "import nosuchmodule_xyz\n"})
    reg = extensions.load(layout)
    assert reg.loaded == [] and reg.errors and not reg.blocking
    events = [json.loads(l) for l in layout.log.read_text(encoding="utf-8").splitlines()]
    hits = [e for e in events if e["event"] == "extension_error"]
    assert len(hits) == 1 and hits[0]["extension"] == "opt" and hits[0]["required"] is False
    assert "did not load" in capsys.readouterr().err
    extensions.load(layout)                                              # cached: not logged twice per process
    assert sum(1 for l in layout.log.read_text(encoding="utf-8").splitlines() if "extension_error" in l) == 1


def test_a_broken_extension_cli_group_is_reported_on_stderr(layout, capsys, monkeypatch):
    _configure(layout.root, ["bad"], {"bad": "import nosuchmodule_xyz\n"})
    from supervisor import cli
    monkeypatch.setattr(cli, "find_root", lambda: layout.root)
    monkeypatch.setattr(cli, "_extensions_cli", {})                 # built on first use (1.10.0)
    cli._extension_group()
    err = capsys.readouterr().err
    assert "bad" in err and "missing from this CLI" in err


def test_doctor_reports_the_framework_checkout_and_refuses_a_dirty_or_untagged_one(layout, monkeypatch):
    from supervisor import updates
    base = {"version": "1.8.0", "path": "/fw", "is_git": True, "commit": "abcdef123456", "tag": "v1.8.0", "dirty": False}
    monkeypatch.setattr(updates, "runtime_checkout", lambda: dict(base))
    c = doctor.check_framework(layout)
    assert c.ok and "abcdef123456" in c.detail and "v1.8.0" in c.detail
    monkeypatch.setattr(updates, "runtime_checkout", lambda: dict(base, dirty=True))
    c = doctor.check_framework(layout)
    assert not c.ok and "uncommitted" in c.detail
    monkeypatch.setattr(updates, "runtime_checkout", lambda: dict(base, tag=None))
    c = doctor.check_framework(layout)
    assert not c.ok and "not at a tag" in c.detail
    monkeypatch.setattr(updates, "runtime_checkout", lambda: dict(base, is_git=False, commit=None, tag=None))
    assert doctor.check_framework(layout).ok                              # an installed package has no checkout to judge
    assert "framework" in {c.name for c in doctor.run(layout)}


# ------------------------------------------------------------------ 1.8.1: review findings on 1.8.0

def test_an_owned_item_stays_closed_to_the_generic_path_after_its_extension_stops_loading(layout):
    """A required extension that fails to import never registers its inbox kinds. Its existing items must not become
    answerable through the generic path (that skips the extension's per-option validation), and the inbox page must
    not reveal rows the extension keeps blind. Built-in kinds keep working so the owner can still act on them."""
    _configure(layout.root, ["rounds"], {"rounds": ROUNDS_EXT})
    item = _labelling_item(layout)
    _configure(layout.root, ["rounds"], {"rounds": "import nosuchmodule_xyz\n" + ROUNDS_EXT})   # the extension breaks
    reg = extensions.load(layout)
    assert reg.blocking and "labelling" not in reg.inbox_kinds
    with pytest.raises(inbox.InboxError, match="cannot be answered.*required extension failed"):
        inbox.answer(layout, item["id"], "ACCEPT")
    out = commands.submit_or_apply(layout, "inbox.answer", {"item_id": item["id"], "response": "ACCEPT"}, "owner")
    assert not out["ok"] and "required extension failed" in out["error"]
    assert inbox.load(layout, item["id"])["status"] == "open"
    gate_item = inbox.create(layout, for_role="owner", subject="g", summary="g", kind="gate",
                             allowed_responses=["APPROVE", "REJECT"])
    assert inbox.answer(layout, gate_item["id"], "APPROVE")["status"] == "answered"       # built-in: still answerable
    client = _client(layout)
    html = client.get("/inbox").text
    assert "0.91" not in html and "identity_mean" not in html                            # still blind
    assert f'action="/inbox/{item["id"]}/answer"' not in html and "required extension failed" in html
    r = client.post(f"/inbox/{item['id']}/answer", data={"csrf_token": CSRF, "response": "ACCEPT"}, follow_redirects=False)
    assert r.status_code == 400 and "required extension failed" in r.text
    assert inbox.load(layout, item["id"])["status"] == "open"


def test_an_unclaimed_kind_with_every_extension_loaded_is_an_ordinary_item(layout):
    """No blocking failure: a kind nobody claims (an extension removed on purpose) is generic data, answerable."""
    _configure(layout.root, [], {})
    item = inbox.create(layout, for_role="owner", subject="old", summary="orphan", kind="labelling")
    assert inbox.ownership_unknown(layout, "labelling") is None
    assert inbox.answer(layout, item["id"], "ACCEPT")["status"] == "answered"


def _git(args, cwd):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
                          env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})


@pytest.mark.skipif(__import__("shutil").which("git") is None, reason="git not on PATH")
def test_runtime_checkout_counts_untracked_files_and_reports_an_uninspectable_checkout(tmp_path, monkeypatch, layout):
    """Against a real tagged repository, not a mocked status: an editable install imports an untracked module in the
    package directory just like a tracked one, so an untracked file is unreviewed runtime code and the checkout is
    not clean. A `git status` that fails is a failed verification, never "clean"."""
    from supervisor import updates
    repo = tmp_path / "fwrepo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
    _git(["init", "-q"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "one"], repo)
    _git(["tag", "-a", "v9.9.9", "-m", "tag"], repo)
    monkeypatch.setattr(updates, "framework_dir", lambda: repo)
    rt = updates.runtime_checkout()
    assert rt["is_git"] and rt["tag"] == "v9.9.9" and rt["dirty"] is False and rt["status_error"] is None
    assert doctor.check_framework(layout).ok
    (repo / "pkg" / "new_runtime_code.py").write_text("y = 2\n", encoding="utf-8")       # untracked, not ignored
    rt = updates.runtime_checkout()
    assert rt["dirty"] is True and rt["untracked"] == ["pkg/new_runtime_code.py"]
    c = doctor.check_framework(layout)
    assert not c.ok and "untracked" in c.detail and "new_runtime_code.py" in c.detail
    (repo / "pkg" / "new_runtime_code.py").unlink()
    (repo / "pkg" / "scratch.pyc").write_bytes(b"\x00")                                 # ignored: not runtime code git tracks
    assert updates.runtime_checkout()["dirty"] is False
    real = updates._git

    def failing(args, cwd, timeout=10):
        if args and args[0] == "status":
            return 128, "fatal: index file corrupt"
        return real(args, cwd, timeout)
    monkeypatch.setattr(updates, "_git", failing)
    rt = updates.runtime_checkout()
    assert rt["dirty"] is None and "index file corrupt" in rt["status_error"]
    c = doctor.check_framework(layout)
    assert not c.ok and "could not be verified" in c.detail
