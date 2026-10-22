"""Gates enforce what they declare: owner_ack_required and evidence_required (2026-10-23 review finding). A gate that
declares neither keeps the legacy "PASSED is enough" behaviour so existing state files keep working."""
import pytest
import yaml

from supervisor import commands, gates, tasks
from supervisor.cli import app
from tests.conftest import TEMPLATE, make_task


def _declare(layout, gate_id, **keys):
    data = yaml.safe_load(layout.gates.read_text(encoding="utf-8"))
    data["gates"][gate_id].update(keys)
    layout.gates.write_text(yaml.safe_dump(data), encoding="utf-8")


def _gate(layout, gate_id):
    return gates.load(layout)["gates"][gate_id]


def test_template_gate_declares_owner_ack_and_evidence():
    data = yaml.safe_load((TEMPLATE / "state" / "gates.yaml").read_text(encoding="utf-8"))
    g = data["gates"]["G0"]
    assert g["owner_ack_required"] is True and g["evidence_required"] is True and g["status"] == "OPEN"


def test_legacy_gate_without_requirement_keys_opens_on_passed_alone(layout):
    gates.set_status(layout, "G0", "PASSED", actor="agent")
    assert gates.phase_open(layout, 1) is True


def test_agent_cannot_pass_an_owner_gate(layout):
    _declare(layout, "G0", owner_ack_required=True)
    with pytest.raises(gates.GateError, match="only an owner"):
        gates.set_status(layout, "G0", "PASSED", ["docs/report.md"], actor="agent")
    assert _gate(layout, "G0")["status"] == "OPEN"            # refused up front: nothing written
    assert gates.phase_open(layout, 1) is False


def test_agent_cannot_pass_an_owner_gate_through_the_command_bus(layout):
    _declare(layout, "G0", owner_ack_required=True)
    out = commands.submit_or_apply(layout, "gate.set", {"gate_id": "G0", "status": "PASSED", "evidence": ["x"]}, actor="cli")
    assert out["ok"] is False and "only an owner" in out["error"]
    assert _gate(layout, "G0")["status"] == "OPEN"


def test_owner_pass_with_evidence_opens_the_phase(layout):
    _declare(layout, "G0", owner_ack_required=True, evidence_required=True)
    make_task(layout, "P1", phase=1)
    assert tasks.eligible(layout) == []
    gates.set_status(layout, "G0", "PASSED", ["docs/report.md"], actor="owner-ui")
    g = _gate(layout, "G0")
    assert g["status"] == "PASSED" and g["evidence"] == ["docs/report.md"]
    assert g["owner_ack"]["by"] == "owner-ui"                 # the owner's own pass doubles as the acknowledgement
    assert gates.phase_open(layout, 1) is True
    assert [t.id for t in tasks.eligible(layout)] == ["P1"]


def test_evidence_gate_refuses_passed_without_evidence(layout):
    _declare(layout, "G0", evidence_required=True)
    with pytest.raises(gates.GateError, match="evidence"):
        gates.set_status(layout, "G0", "PASSED", [], actor="owner")
    assert _gate(layout, "G0")["status"] == "OPEN"
    gates.set_status(layout, "G0", "PASSED", ["runs/T1/a1/report.md"], actor="owner")
    assert gates.phase_open(layout, 1) is True


def test_passed_without_owner_ack_is_not_open_when_required(layout):
    """State written behind the library's back (hand edit, old tool): PASSED alone never opens an owner gate."""
    _declare(layout, "G0", owner_ack_required=True, evidence_required=True, status="PASSED", evidence=["x"], owner_ack=None)
    assert gates.phase_open(layout, 1) is False
    assert "owner acknowledgement required" in " ".join(gates.requirements_unmet(_gate(layout, "G0")))
    _declare(layout, "G0", owner_ack={"by": "agent", "at": "now", "note": ""})     # an agent's ack does not count
    assert gates.phase_open(layout, 1) is False
    gates.ack(layout, "G0", actor="owner", note="reviewed")
    assert gates.phase_open(layout, 1) is True


def test_passed_without_evidence_is_not_open_when_required(layout):
    _declare(layout, "G0", evidence_required=True, status="PASSED", evidence=[])
    assert gates.phase_open(layout, 1) is False
    assert gates.gate_open({"status": "PASSED", "evidence_required": True, "evidence": ["x"]}) is True
    assert gates.gate_open({"status": "PASSED"}) is True
    assert gates.gate_open(None) is False


def test_only_an_owner_may_acknowledge(layout):
    with pytest.raises(gates.GateError, match="only an owner"):
        gates.ack(layout, "G0", actor="engineer")
    assert _gate(layout, "G0").get("owner_ack") is None
    gates.ack(layout, "G0", actor="owner-ui", note="ok")
    assert _gate(layout, "G0")["owner_ack"]["by"] == "owner-ui"


def test_failed_and_open_never_need_owner_or_evidence(layout):
    _declare(layout, "G0", owner_ack_required=True, evidence_required=True)
    gates.set_status(layout, "G0", "FAILED", actor="agent")
    assert _gate(layout, "G0")["status"] == "FAILED"
    gates.set_status(layout, "G0", "OPEN", actor="agent")
    assert _gate(layout, "G0")["status"] == "OPEN"
    with pytest.raises(gates.GateError):
        gates.set_status(layout, "G0", "MAYBE", actor="owner")


def test_cli_gate_set_reports_a_clear_error_for_an_agent_on_an_owner_gate(layout):
    from typer.testing import CliRunner
    _declare(layout, "G0", owner_ack_required=True)
    r = CliRunner().invoke(app, ["gate", "set", "G0", "PASSED", "--evidence", "x"])
    assert r.exit_code != 0 and "only an owner" in r.output
    assert _gate(layout, "G0")["status"] == "OPEN"
    r = CliRunner().invoke(app, ["gate", "set", "G0", "PASSED", "--evidence", "x", "--actor", "owner"])
    assert r.exit_code == 0, r.output
    assert gates.phase_open(layout, 1) is True
