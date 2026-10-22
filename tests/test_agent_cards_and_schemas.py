"""Every active role in the template has a card, every schema validates its samples, and routing.yaml covers
every role the template names. The forbidden-token check is the executable form of the acceptance
criterion "no model names appear in cards" — routing.yaml is the only place a model or vendor is named."""
from __future__ import annotations

import json
import pathlib
import re

import pytest
import yaml
from jsonschema import Draft202012Validator

from supervisor.adapters import REGISTRY
from supervisor.adapters.claude_cli import DEFAULT_TOOLS, role_profile
from supervisor._vendor import contract
from supervisor.paths import Layout

ROOT = pathlib.Path(__file__).resolve().parents[1] / "supervisor" / "template"
AGENTS = ROOT / "agents"
SCHEMAS = AGENTS / "schemas"
SAMPLES = SCHEMAS / "samples"
SCHEMA_NAMES = ("delivery", "review", "research_report", "assessment")
CARD_WORD_LIMIT = 400

# Model and vendor names. A card describes judgement; config/routing.yaml decides who serves it.
FORBIDDEN_IN_CARDS = ("opus", "sonnet", "haiku", "claude", "codex", "chatgpt", "openai", "anthropic",
                      "gpt-", "gpt4", "qwen", "llama", "gemini", "mistral", "deepseek", "flux", "sdxl")


def routing_doc() -> dict:
    return yaml.safe_load((ROOT / "config" / "routing.yaml").read_text(encoding="utf-8"))


def roles() -> dict:
    return routing_doc()["roles"]


def store_roles() -> set:
    """Every role id the task store actually dispatches to or reviews with."""
    found = set()
    for path in sorted((ROOT / "state" / "tasks").glob("T*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        found.add(doc["owner_role"])
        found.update(doc.get("reviewer") or [])
    return {r for r in found if r != "none"}


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ routing.yaml

def test_routing_loads_with_the_blocks_the_framework_reads():
    doc = routing_doc()
    assert doc["version"] == 1
    assert doc["capacity"]["claude"]["reserve_percent"] == 20
    assert set(doc["cost_lanes"]) == {"deterministic", "local", "claude", "codex", "human", "mixed", "paid"}
    assert doc["claude"]["supports_effort"] is True and doc["limits"]["max_agent_jobs"] in (1, 2)
    assert doc["limits"]["max_resource_jobs"] == 1 and doc["verification"]["allowed_test_commands"]


def test_routing_covers_every_role_in_the_task_store():
    missing = store_roles() - set(roles())
    assert not missing, f"roles dispatched by the task store with no routing entry: {sorted(missing)}"


@pytest.mark.parametrize("role", sorted(roles()))
def test_every_routing_entry_resolves(role):
    entry = roles()[role]
    assert entry["adapter"] in REGISTRY, f"{role}: unknown adapter {entry['adapter']!r}"
    assert isinstance(entry["active"], bool) and isinstance(entry["active_from"], int)
    assert set(entry.get("result_kinds") or []) <= set(SCHEMA_NAMES), f"{role}: unknown result kind"
    prof = role_profile(Layout(ROOT), role)
    tools = prof["tools"]
    assert tools in DEFAULT_TOOLS or isinstance(tools, list), f"{role}: unresolvable tools {tools!r}"


def test_model_bearing_roles_declare_a_model_and_the_rest_do_not():
    for role, entry in roles().items():
        if entry["adapter"] in ("claude_cli", "codex_cli"):
            assert entry.get("model") and entry.get("effort"), f"{role}: agent lane without model/effort"
        else:
            assert "model" not in entry, f"{role}: {entry['adapter']} lane must not name a model"


def test_code_review_runs_on_a_different_lane_than_the_implementers():
    """Reviewer independence is a vendor split, with the fallback flagged as same-vendor."""
    entry = roles()["code_review"]
    assert entry["adapter"] != roles()["engineer"]["adapter"]
    assert entry["sandbox"] == "read-only" and entry["tools"] == "reader"
    assert entry["fallback"]["flag"] == "same_vendor"


# ------------------------------------------------------------------ cards

def active_carded_roles() -> dict:
    return {r: e for r, e in roles().items() if e["active"] and e["card"]}


def test_the_template_roles_are_active_and_carded():
    expected = {"senior_pm", "chief_architect", "engineer", "code_review", "research_strategist"}
    assert set(active_carded_roles()) == expected


def test_dormant_roles_have_no_card_and_a_reason():
    for role, entry in roles().items():
        if entry["card"] is None and role != "owner":
            assert entry["note"], f"{role}: card: null without a note saying when the card comes"


@pytest.mark.parametrize("role", sorted(active_carded_roles()))
def test_card_exists_and_is_short(role):
    card = AGENTS / roles()[role]["card"]
    assert card.exists(), f"{role}: missing card {card.name}"
    words = len(card.read_text(encoding="utf-8").split())
    assert words <= CARD_WORD_LIMIT, f"{role}: card is {words} words (limit {CARD_WORD_LIMIT})"


@pytest.mark.parametrize("role", sorted(active_carded_roles()))
def test_card_names_no_model_or_vendor(role):
    """No model names appear in cards."""
    text = (AGENTS / roles()[role]["card"]).read_text(encoding="utf-8").lower()
    hits = sorted({t for t in FORBIDDEN_IN_CARDS if t in text})
    assert not hits, f"{role}: card names a model or vendor: {hits}"


@pytest.mark.parametrize("role", sorted(active_carded_roles()))
def test_card_points_at_the_schemas_it_must_produce(role):
    text = (AGENTS / roles()[role]["card"]).read_text(encoding="utf-8")
    kinds = roles()[role]["result_kinds"]
    assert kinds, f"{role}: carded role with no result_kinds"
    assert any(k in text for k in kinds), f"{role}: card never names any of its result kinds {kinds}"


# ------------------------------------------------------------------ schemas

@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_is_valid_draft_2020_12(name):
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["title"] == name
    assert schema["properties"]["kind"]["const"] == name
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["additionalProperties"] is False, f"{name}: must be closed so a typo cannot pass silently"


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_valid_sample_validates(name):
    errors = sorted(Draft202012Validator(load_schema(name)).iter_errors(
        json.loads((SAMPLES / f"{name}.valid.json").read_text(encoding="utf-8"))), key=lambda e: e.json_path)
    assert not errors, f"{name}.valid.json: " + "; ".join(f"{e.json_path}: {e.message}" for e in errors)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_invalid_sample_is_rejected(name):
    doc = json.loads((SAMPLES / f"{name}.invalid.json").read_text(encoding="utf-8"))
    assert not Draft202012Validator(load_schema(name)).is_valid(doc), \
        f"{name}.invalid.json validated: the schema has lost a constraint (see agents/schemas/samples/README.md)"


def test_review_schema_enums_match_the_contract():
    """The parser in supervisor/_vendor/contract.py is the runtime authority; the schema must not drift from it."""
    schema = load_schema("review")
    assert tuple(schema["properties"]["verdict"]["enum"]) == contract.REVIEW_VERDICTS
    assert set(schema["$defs"]["finding"]["properties"]["category"]["enum"]) == contract.FINDING_CATEGORIES


def test_review_schema_encodes_the_contract_cross_field_rules():
    """ACCEPT with a blocking finding, and a blocking advisory on a non-BLOCKED verdict, must fail both ways."""
    validator = Draft202012Validator(load_schema("review"))
    blocking = {"category": "correctness", "blocking": True, "message": "m", "evidence": "f.py:1"}
    advisory = {"category": "advisory", "blocking": True, "message": "m", "evidence": "f.py:1"}
    base = {"kind": "review", "schema_version": 1}

    def both(doc):
        text = "```json\n" + json.dumps(doc) + "\n```"
        with pytest.raises(ValueError):
            contract.review_result(text)
        assert not validator.is_valid(doc)

    both({**base, "verdict": "ACCEPT", "findings": [blocking]})
    both({**base, "verdict": "REVISE", "findings": [advisory]})
    # the same shapes that the contract accepts must validate
    for ok in ({**base, "verdict": "ACCEPT", "findings": []},
               {**base, "verdict": "REVISE", "findings": [blocking]},
               {**base, "verdict": "BLOCKED", "findings": [advisory]}):
        contract.review_result("```json\n" + json.dumps(ok) + "\n```")
        assert validator.is_valid(ok), ok["verdict"]


def test_delivery_schema_matches_the_prompt_result_contract_and_the_adapter():
    """default_brief() tells agents which keys to send, and the adapter adds permission_denials before
    writing result.json; the schema has to accept exactly that object."""
    schema = load_schema("delivery")
    assert set(schema["required"]) == {"kind", "schema_version", "status", "artifacts", "notes", "decisions"}
    validator = Draft202012Validator(schema)
    obj = {"kind": "delivery", "schema_version": 1, "status": "COMPLETE",
           "artifacts": ["agents/engineer.md"], "notes": "n", "decisions": [],
           "permission_denials": ["Bash(git push origin main)"]}
    assert validator.is_valid(obj)
    assert not validator.is_valid({**obj, "artifacts": ["D:\\Projects\\demo\\agents\\engineer.md"]})
    assert not validator.is_valid({**obj, "status": "OK"})


def test_delivery_statuses_are_exactly_the_ones_the_adapter_classifies():
    from supervisor.adapters.claude_cli import classify_status
    for status in load_schema("delivery")["properties"]["status"]["enum"]:
        assert classify_status({"status": status}) == status

