"""Hook contracts (phase 2 step 7): the reference extension (examples/reference_extension/reference), copied into a
temporary project and loaded through the real loader as a project would, uses every registry hook through
supervisor.ext only. Each hook's contract is checked through it, and a hook added to ext.Registry without a check
here fails test_every_documented_hook_is_exercised."""
from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from supervisor import doctor, ext, extensions, inbox, policy, render, tasks, testing
from supervisor.adapters import REGISTRY
from supervisor.adapters.python import JOBS
from supervisor.models.task import TaskStatus

REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "examples" / "reference_extension" / "reference"
ROUTING = {"reference": {"forbidden_word": "CLASSIFIED"}, "gpu": {"usable_vram_floor_mb": 4000}}


@pytest.fixture
def lay(layout):
    shutil.copytree(REFERENCE, layout.extensions / "reference", ignore=shutil.ignore_patterns("__pycache__"))
    testing.add_extensions(layout, {}, entries=["reference"])
    write_routing(layout, ROUTING)
    reg = extensions.load(layout)
    assert reg.errors == [] and reg.loaded == ["reference"], reg.errors
    yield layout
    testing.reset()


def write_routing(layout, doc):
    layout.config.mkdir(parents=True, exist_ok=True)
    import yaml
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def note(layout, subject="first") -> str:
    """A note, as one of the extension's jobs would open it."""
    return inbox.create(layout, for_role="owner", subject=subject, summary="annotate me", kind="reference_note")["id"]


def log_lines(layout) -> list[dict]:
    return [json.loads(line) for line in layout.log.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- the extension itself

def test_the_reference_extension_imports_only_the_public_api():
    public = ("supervisor.ext", "supervisor.testing")
    seen = 0
    for path in REFERENCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module == "supervisor":
                targets = [f"supervisor.{a.name}" for a in node.names]   # from supervisor import ext
            elif isinstance(node, ast.ImportFrom) and not node.level:
                targets = [node.module or ""]
            else:
                continue
            for name in targets:
                if name == "supervisor" or name.startswith("supervisor."):
                    seen += 1
                    assert any(name == p or name.startswith(p + ".") for p in public), f"{path.name} imports {name}"
    assert seen >= 3


@pytest.mark.parametrize("module", ["reference", "reference.lane", "reference.pages"])
def test_importing_any_of_its_modules_registers_nothing(lay, module):
    assert testing.import_side_effects(lay, module) == {"adapters": [], "python_jobs": [], "commands": []}


def test_every_documented_hook_is_exercised(lay):
    reg = extensions.load(lay)
    registered = {
        "config_sections": reg.config_owners == {"reference": "reference"},
        "adapter": reg.adapters == {"reference_lane": "reference"} and "reference_lane" in REGISTRY,
        "python_job": reg.python_jobs == {"reference_count": "reference"} and "reference_count" in JOBS,
        "doctor_check": len(reg.doctor_checks) == 1,
        "overview_panel": len(reg.overview_panels) == 1,
        "packet_screen": len(reg.packet_screens) == 1,
        "projection": "REFERENCE.md" in reg.projection_paths,
        "cli": [name for _app, name in reg.cli_apps] == ["reference"],
        "command": sorted(reg.commands) == ["reference.close", "reference.note"],
        "inbox_kind": reg.inbox_kind_owners == {"reference_note": "reference"},
        "ui_router": [(label, path) for _r, label, path in reg.ui_routers] == [("Reference", "/reference")],
        "template_dir": [p.name for p in reg.template_dirs] == ["templates"],
        "static_dir": [prefix for prefix, _p in reg.static_dirs] == ["reference"],
    }
    hooks = sorted(n for n, v in vars(ext.Registry).items() if not n.startswith("_") and callable(v))
    assert hooks == sorted(registered), "a hook in ext.Registry has no contract check here (or one went away)"
    assert all(registered.values()), [h for h, ok in registered.items() if not ok]


# ---------------------------------------------------------------- each hook's contract

def test_adapter_its_lane_completes_a_task_through_the_loop(lay):
    testing.make_task(lay, "L", job={"adapter": "reference_lane", "output": "out/l.txt"}, artifacts=["out/l.txt"])
    testing.drive(lay, iterations=40, until=lambda: tasks.load(lay, "L").status == TaskStatus.COMPLETE)
    assert tasks.load(lay, "L").status == TaskStatus.COMPLETE
    assert (lay.root / "out" / "l.txt").read_text(encoding="utf-8") == "made by the reference lane\n"


def test_adapter_a_paused_lane_defers_without_spending_an_attempt(lay):
    write_routing(lay, {**ROUTING, "reference": {"paused": True}})
    testing.make_task(lay, "P", job={"adapter": "reference_lane"}, artifacts=["reference_out.txt"])
    testing.drive(lay, iterations=4)
    from supervisor import jobs
    recs = jobs.for_task(lay, "P")
    assert len(recs) == 1 and recs[0]["phase"] == "PREPARED" and int(recs[0].get("launch_attempts", 0)) == 0
    assert "paused" in recs[0]["deferred_reason"]


def test_python_job_runs_through_the_python_lane(lay):
    note(lay)
    testing.make_task(lay, "C", job={"adapter": "python", "fn": "reference_count", "params": {"out": "count.json"}},
                      artifacts=["count.json"])
    testing.drive(lay, until=lambda: tasks.load(lay, "C").status == TaskStatus.COMPLETE)
    assert tasks.load(lay, "C").status == TaskStatus.COMPLETE
    assert json.loads((lay.root / "count.json").read_text(encoding="utf-8"))["open"] == 1


def test_doctor_check_reports_through_doctor(lay):
    agent = next(name for name, a in REGISTRY.items() if hasattr(a, "agent_handle"))
    write_routing(lay, {**ROUTING, "reference": {**ROUTING["reference"], "agent_lane": agent}})
    check = next(c for c in doctor.run(lay) if c.name == "reference")
    assert check.ok and "GPU floor 4000" in check.detail and f"agent lane {agent}" in check.detail
    write_routing(lay, {**ROUTING, "reference": {"agent_lane": "no_such_lane"}})
    check = next(c for c in doctor.run(lay) if c.name == "reference")
    assert not check.ok and "no_such_lane" in check.detail


def test_overview_panel_reaches_the_overview(lay):
    note(lay)
    from supervisor.ui.routes.overview import overview_context
    panel = next(p for p in overview_context(lay)["panels"] if p["title"] == "Reference notes")
    assert panel["rows"] == [("open", "1"), ("closed", "0")]


def test_packet_screen_refuses_a_brief(lay):
    policy.assert_packet_clean("an ordinary brief", lay, "packet")
    with pytest.raises(policy.PolicyViolation, match="reference extension"):
        policy.assert_packet_clean("this is CLASSIFIED", lay, "packet")


def test_projection_renders_a_view_verification_ignores(lay):
    note(lay)
    paths = render.render_all(lay)
    assert any(p.name == "REFERENCE.md" for p in paths)
    assert "(open): 0 annotations" in (lay.root / "REFERENCE.md").read_text(encoding="utf-8")
    assert "REFERENCE.md" in extensions.load(lay).projection_paths


def test_cli_group_runs_as_a_supervisor_command(lay):
    note(lay)
    from supervisor.cli import app
    result = CliRunner().invoke(app, ["reference", "count"])
    assert result.exit_code == 0, result.output
    assert "1 open notes" in result.output


def test_command_and_inbox_kind_the_note_is_the_extensions_to_change_and_close(lay):
    item = note(lay)
    with pytest.raises(inbox.InboxError, match="finalised by its extension"):
        inbox.answer(lay, item, "DONE")                              # the generic path may not close it
    first = ext.submit_command(lay, "reference.note", {"item_id": item, "text": "looks right"}, command_id="n-1")
    again = ext.submit_command(lay, "reference.note", {"item_id": item, "text": "looks right"}, command_id="n-1")
    assert first["result"] == {"annotations": 1} and again["replayed"]
    assert len(inbox.load(lay, item)["notes"]) == 1
    assert next(e for e in log_lines(lay) if e["event"] == "reference_note")["actor"] == "reference:owner-ui"
    closed = ext.submit_command(lay, "reference.close", {"item_id": item})
    assert closed["result"] == {"status": "answered"} and inbox.load(lay, item)["answer"]["response"] == "DONE"


def test_ui_router_template_and_static_files(lay):
    item = note(lay, subject="the page")
    client = testing.ui_client(lay)
    html = client.get("/reference").text
    assert "the page" in html and "<nav" in html and 'href="/reference"' in html
    assert client.get("/static/reference/reference.css").status_code == 200
    assert client.post(f"/reference/{item}/note", data={"text": "x", "csrf_token": "forged"}).status_code == 403
    for _ in range(2):                                                # the same form posted twice
        r = client.post(f"/reference/{item}/note", data={"text": "from the page", "form_id": "f-1",
                                                         "csrf_token": testing.CSRF}, follow_redirects=False)
        assert r.status_code == 303 and "flash=applied" in r.headers["location"]
    assert [n["text"] for n in inbox.load(lay, item)["notes"]] == ["from the page"]
    assert client.post("/reference/H-999/note", data={"text": "x", "csrf_token": testing.CSRF}).status_code == 404
    r = client.post(f"/reference/{item}/close", data={"csrf_token": testing.CSRF}, follow_redirects=False)
    assert r.status_code == 303 and inbox.load(lay, item)["status"] == "answered"
