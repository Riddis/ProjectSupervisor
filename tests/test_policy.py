"""The policy boundary — credential isolation, log redaction, plugin packet screens."""
import json
from pathlib import Path

import pytest
import yaml

from supervisor import packets, policy, tasks
from supervisor.adapters import REGISTRY, claude_api
from supervisor.adapters.process import scrub_env
from tests.conftest import make_task

# A key-shaped string (matches policy's anthropic pattern) and a value that looks like nothing at all: the
# second one proves the `secrets/` literal check works without any pattern recognising it.
SHAPED_KEY = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8" * 2
UNSHAPED_VALUE = "correct-horse-battery-staple-7"
# Base64 padding makes a bare token look like `KEY=` with an empty value (review a4 finding 3).
PADDED_TOKEN = "cmVmcmVzaC10b2tlbi1mb3ItdGVzdHM=="
# A synthetic private key: body lines that are not real key material, but are laid out like one. The marker is
# only the first line of the secret — redaction has to take the body and the END line with it.
PEM_BODY = ["MIIEogIBAAKCAQEAZmFrZS1rZXktbWF0ZXJpYWwtZm9yLXRlc3RzLW9ubHktbm90",
            "LWEtcmVhbC1rZXktYXQtYWxsLXBsZWFzZS1kby1ub3QtdHJ5LXRvLXVzZS1pdA=="]
PEM = "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(PEM_BODY) + "\n-----END RSA PRIVATE KEY-----"

def write_policy(layout, doc):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "policy.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def write_secret(layout, key="SERVICE_TOKEN", value=UNSHAPED_VALUE):
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / ".env").write_text(f"# comment\n{key}={value}\n", encoding="utf-8")
    return value


# ---------------------------------------------------------------------------- fail-closed defaults

def test_defaults_apply_when_the_config_file_is_missing(layout):
    assert not (layout.config / "policy.yaml").exists()
    assert policy.secret_patterns(layout)
    assert policy.env_key_suffixes(layout) == tuple(policy.DEFAULTS["secrets"]["env_key_suffixes"])


def test_the_template_config_is_loadable_with_every_entry_in_effect(layout, monkeypatch):
    from tests.conftest import TEMPLATE
    real = type(layout)(TEMPLATE)
    doc = policy.load(real)
    assert policy.config_problems(real) == []        # the shipped file says what it means, every entry in effect
    assert doc["secrets"]["dir"] == "secrets"
    assert any(name == "private_key_block" for name, _ in policy.secret_patterns(real))


def test_a_config_edit_cannot_remove_a_builtin_guard(layout):
    write_policy(layout, {"version": 1, "secrets": {"patterns": [], "env_key_suffixes": []}})
    assert any(name == "anthropic_api_key" for name, _ in policy.secret_patterns(layout))
    assert "_TOKEN" in policy.env_key_suffixes(layout)


def test_a_malformed_config_does_not_open_anything(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "policy.yaml").write_text("secrets: [this is not a mapping\n", encoding="utf-8")
    assert policy.secret_patterns(layout)
    assert any("malformed YAML" in p for p in policy.config_problems(layout))


# A config file that *parses* but says the wrong kind of thing is the dangerous case: `secrets: {patterns: null}`
# used to replace the built-in list with nothing, leaving the packet screen with no protections at all.
WRONG_KIND = [
    ({"secrets": {"patterns": None}}, "secrets.patterns"),
    ({"secrets": None}, "secrets"),
    ({"secrets": {"env_key_suffixes": "_TOKEN"}}, "secrets.env_key_suffixes"),
    ({"secrets": {"min_value_length": "none"}}, "secrets.min_value_length"),
]


@pytest.mark.parametrize("doc, rejected", WRONG_KIND)
def test_a_wrongly_typed_entry_is_rejected_and_the_floor_stands(layout, doc, rejected):
    write_policy(layout, {"version": 1, **doc})
    assert [p.split(":")[0] for p in policy.config_problems(layout)] == [rejected]
    # every protection the entry could have deleted is still in place
    assert any(name == "anthropic_api_key" for name, _ in policy.secret_patterns(layout))
    assert "pattern/anthropic_api_key" in policy.find_secrets(f"key {SHAPED_KEY}", layout)
    assert policy.env_key_suffixes(layout) == tuple(policy.DEFAULTS["secrets"]["env_key_suffixes"])
    assert policy.redact(f"key {SHAPED_KEY}", layout) == "key [REDACTED]"


def test_an_entry_with_no_builtin_floor_is_still_taken_as_given(layout):
    """The kind check guards the floor; it must not turn a project's own section into an error, or every
    forward-compatible config edit becomes a blocker."""
    write_policy(layout, {"version": 1, "my_plugin": {"channels": {"fanclub": {"allowed": False}}}})
    assert policy.config_problems(layout) == []
    assert policy.section("my_plugin", layout)["channels"]["fanclub"]["allowed"] is False


def test_a_null_item_inside_a_list_contributes_no_name(layout):
    write_policy(layout, {"version": 1, "secrets": {"env_key_suffixes": [None, ""]}})
    assert policy.config_problems(layout) == []                  # a list is a list; its items are filtered
    assert "" not in policy.env_key_suffixes(layout)             # endswith("") would strip every variable
    assert scrub_env({"PATH": "p", "ANTHROPIC_API_KEY": SHAPED_KEY}, layout) == {"PATH": "p"}


# ---------------------------------------------------------------------------- packets never carry a credential

def test_a_packet_containing_a_secret_like_string_is_refused(layout):
    make_task(layout, "T")
    def fn(doc):
        doc["objective"] = f"call the API with {SHAPED_KEY} and report back"
    tasks._update(layout, "T", fn)
    task = tasks.load(layout, "T")
    with pytest.raises(policy.PolicyViolation) as exc:
        packets.delivery_brief(layout, task)
    assert "pattern/anthropic_api_key" in str(exc.value)
    assert SHAPED_KEY not in str(exc.value)                      # the refusal never repeats the credential


def test_a_packet_containing_a_literal_value_from_secrets_is_refused(layout):
    value = write_secret(layout)
    assert policy.find_secrets(f"the token is {value}", layout) == ["secrets/.env:SERVICE_TOKEN"]
    make_task(layout, "T")
    def fn(doc):
        doc["acceptance"] = f"posted with {value}"
    tasks._update(layout, "T", fn)
    with pytest.raises(policy.PolicyViolation):
        packets.delivery_brief(layout, tasks.load(layout, "T"))


def test_a_review_packet_and_a_revision_packet_are_screened_too(layout):
    write_secret(layout)
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    with pytest.raises(policy.PolicyViolation):
        packets.review_brief(layout, task, "d" * 64, [f"note: {UNSHAPED_VALUE}"], "code_review")
    with pytest.raises(policy.PolicyViolation):
        packets.revision_brief(task, "brief", "code_review", f"the test printed {UNSHAPED_VALUE}")


def test_a_clean_packet_is_returned_unchanged(layout):
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    brief = packets.delivery_brief(layout, task)
    assert task.objective in brief and policy.find_secrets(brief, layout) == []


def test_a_key_written_a_moment_ago_is_seen_at_once(layout):
    assert policy.find_secrets(f"x {UNSHAPED_VALUE}", layout) == []      # populates the stamp cache
    write_secret(layout)
    assert policy.find_secrets(f"x {UNSHAPED_VALUE}", layout) == ["secrets/.env:SERVICE_TOKEN"]


def test_the_secrets_readme_is_not_treated_as_a_credential_store(layout):
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "README.md").write_text("Put ANTHROPIC_API_KEY=your-key-here in .env\n", encoding="utf-8")
    assert policy.secret_values(layout) == {}


def test_a_json_token_file_is_covered(layout):
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "service_token.json").write_text(
        json.dumps({"token": {"access": UNSHAPED_VALUE}, "expires_in": 5184000}), encoding="utf-8")
    assert policy.find_secrets(f"used {UNSHAPED_VALUE}", layout) == ["secrets/service_token.json:token.access"]


# ---------------------------------------------------------------------------- credential isolation

def test_the_api_key_reaches_the_request_header_and_nothing_else(layout, monkeypatch):
    """The one adapter that uses a credential (claude_api) must keep it out of the spec, the run dir and the
    packet. It is read at call time and put in a header; nothing persisted may contain it."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / ".env").write_text(f"ANTHROPIC_API_KEY={SHAPED_KEY}\n", encoding="utf-8")
    make_task(layout, "CA")
    def fn(doc):
        doc["job"] = {"adapter": "claude_api", "role": "engineer"}
    tasks._update(layout, "CA", fn)
    assert claude_api.api_key(layout) == SHAPED_KEY               # it IS available to the adapter

    adapter = REGISTRY["claude_api"]
    spec = adapter.prepare(layout, tasks.load(layout, "CA"), "CA.a1")
    assert SHAPED_KEY not in json.dumps(spec)
    for path in sorted((layout.runs / "CA" / "CA.a1").rglob("*")):
        if path.is_file():
            assert SHAPED_KEY not in path.read_text(encoding="utf-8", errors="replace"), path

    sent = {}
    # Patch the instance and the transport: `monkeypatch.setattr(instance, ...)` elsewhere in the suite leaves a
    # shadowing instance attribute behind, so a class-level patch here would be bypassed and the call would go
    # out over the network for real.
    monkeypatch.setattr(claude_api.requests, "post", _no_network)
    monkeypatch.setattr(adapter, "_post", lambda url, headers, payload, timeout:
                        sent.update(headers=headers, payload=payload) or _Resp())
    handle = adapter.submit(spec)
    assert sent, json.loads(Path(handle["result_path"]).read_text(encoding="utf-8"))
    assert sent["headers"]["x-api-key"] == SHAPED_KEY             # only here
    assert SHAPED_KEY not in json.dumps(sent["payload"])


def _no_network(*args, **kwargs):
    raise AssertionError("a test must never reach the network")


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"content": [{"type": "text", "text": "```json\n" + json.dumps(
            {"kind": "delivery", "schema_version": 1, "status": "COMPLETE", "artifacts": [], "tests": [],
             "notes": "n", "decisions": []}) + "\n```"}], "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_agent_children_never_receive_a_secret_environment_variable():
    env = scrub_env({"PATH": "p", "ANTHROPIC_API_KEY": SHAPED_KEY, "SERVICE_ACCESS_TOKEN": "t", "HOME": "h"})
    assert env == {"PATH": "p", "HOME": "h"}


@pytest.mark.parametrize("suffix", policy.DEFAULTS["secrets"]["env_key_suffixes"])
def test_every_documented_env_suffix_is_actually_stripped(suffix):
    """One case per suffix in the policy data, so removing one from DEFAULTS (or letting scrub_env's filter drift
    away from it, as `_PASSWORD` did) fails here instead of silently leaking into every child."""
    name = f"VENDOR{suffix}"
    assert scrub_env({"PATH": "p", name: UNSHAPED_VALUE}) == {"PATH": "p"}


def test_a_configured_env_suffix_reaches_both_the_child_filter_and_the_screen(layout, monkeypatch):
    """`secrets.env_key_suffixes` is live config, not decoration: one edit tightens scrub_env and the packet
    screen together."""
    write_policy(layout, {"version": 1, "secrets": {"env_key_suffixes": ["_HANDOFF_KEY"]}})
    monkeypatch.setenv("VENDOR_HANDOFF_KEY", UNSHAPED_VALUE)
    assert "_HANDOFF_KEY" in policy.env_key_suffixes(layout)
    assert scrub_env({"PATH": "p", "VENDOR_HANDOFF_KEY": UNSHAPED_VALUE}, layout) == {"PATH": "p"}
    assert policy.find_secrets(f"used {UNSHAPED_VALUE}", layout) == ["env/VENDOR_HANDOFF_KEY"]


# ---------------------------------------------------------------------------- the knobs that would loosen

def test_a_huge_min_value_length_cannot_disable_the_literal_check(layout):
    write_policy(layout, {"version": 1, "secrets": {"min_value_length": 100000}})
    write_secret(layout)
    assert policy.find_secrets(f"the token is {UNSHAPED_VALUE}", layout) == ["secrets/.env:SERVICE_TOKEN"]


def test_ignore_files_cannot_hide_the_file_that_holds_the_keys(layout):
    write_policy(layout, {"version": 1, "secrets": {"ignore_files": [".env", ".env.local", "notes.txt"]}})
    write_secret(layout)
    assert policy.find_secrets(UNSHAPED_VALUE, layout) == ["secrets/.env:SERVICE_TOKEN"]
    assert ".env" not in policy._clamped_secrets(layout)["ignore_files"]
    assert "notes.txt" in policy._clamped_secrets(layout)["ignore_files"]      # anything else is the owner's call


def test_an_empty_redaction_placeholder_falls_back_to_the_builtin(layout):
    write_policy(layout, {"version": 1, "secrets": {"redaction_placeholder": "  "}})
    assert policy.placeholder(layout) == "[REDACTED]"
    assert policy.redact(f"key {SHAPED_KEY}", layout) == "key [REDACTED]"


# ------------------------------------------------- the literal scanner is a credential scanner, not a value scanner

def test_non_secret_config_beside_the_keys_does_not_make_a_brief_undispatchable(layout):
    """A real .env carries an account id and a base URL next to the token. Refusing every 8+ character value
    would make any brief that mentions the account id un-dispatchable, with no way to say which value was at
    fault — so a pair counts only when its key, its file or its shape says credential."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / ".env").write_text(
        "SERVICE_USER_ID=17841400000000000\n"
        "SERVICE_HANDLE=demo.account\n"
        "SERVICE_BASE_URL=https://api.example-service.test/v1\n"
        f"SERVICE_ACCESS_TOKEN={UNSHAPED_VALUE}\n", encoding="utf-8")
    assert list(policy.secret_values(layout)) == [".env:SERVICE_ACCESS_TOKEN"]
    make_task(layout, "T")
    def fn(doc):
        doc["objective"] = "post to account 17841400000000000 via https://api.example-service.test/v1 as demo.account"
    tasks._update(layout, "T", fn)
    packets.delivery_brief(layout, tasks.load(layout, "T"))        # no refusal
    assert policy.find_secrets(f"token {UNSHAPED_VALUE}", layout) == ["secrets/.env:SERVICE_ACCESS_TOKEN"]


def test_a_key_shaped_value_under_an_innocent_name_is_still_a_secret(layout):
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / ".env").write_text(f"NOTE={SHAPED_KEY}\n", encoding="utf-8")
    assert list(policy.secret_values(layout)) == [".env:NOTE"]


def test_every_value_in_a_dedicated_credential_file_counts(layout):
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "service_token.json").write_text(
        json.dumps({"value": UNSHAPED_VALUE, "scope": "read,publish"}), encoding="utf-8")
    assert sorted(policy.secret_values(layout)) == [
        "service_token.json:scope", "service_token.json:value"]


def test_a_bare_token_counts_by_its_file_name_and_a_bare_note_does_not(layout):
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "notes.txt").write_text("rotation-2026-11-12\n", encoding="utf-8")
    (layout.secrets / "refresh_token.txt").write_text(f"{UNSHAPED_VALUE}\n", encoding="utf-8")
    assert list(policy.secret_values(layout)) == ["refresh_token.txt:#0"]


@pytest.mark.parametrize("token", [PADDED_TOKEN, PADDED_TOKEN.rstrip("="), "a" * 8 + "=", "QUJD=REVG=="])
def test_base64_padding_does_not_turn_a_bare_token_into_an_empty_assignment(layout, token):
    """`cmVm…==` splits on its first `=` into a plausible key and a too-short value, so the whole line used to
    be collected as nothing: a credential file became invisible to all three guards (review a4 finding 3)."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "refresh_token.txt").write_text(f"{token}\n", encoding="utf-8")
    assert policy.secret_values(layout) == {"refresh_token.txt:#0": token}
    assert policy.find_secrets(f"refreshed with {token}", layout) == ["secrets/refresh_token.txt:#0"]
    assert policy.redact(f"refreshed with {token}", layout) == "refreshed with [REDACTED]"
    make_task(layout, "B")
    def fn(doc):
        doc["objective"] = f"refresh using {token}"
    tasks._update(layout, "B", fn)
    with pytest.raises(policy.PolicyViolation):
        packets.delivery_brief(layout, tasks.load(layout, "B"))


def test_a_padded_value_in_an_assignment_is_still_read_as_the_value(layout):
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / ".env").write_text(f"SERVICE_ACCESS_TOKEN={PADDED_TOKEN}\n", encoding="utf-8")
    assert policy.secret_values(layout) == {".env:SERVICE_ACCESS_TOKEN": PADDED_TOKEN}


def test_a_short_assignment_is_not_mistaken_for_a_token_in_an_ordinary_dotenv(layout):
    """The bare-token fallback must not make ordinary `.env` config collectable: the file is not a dedicated
    credential file, so a line that is not a credential by key, shape or file name still counts for nothing."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / ".env").write_text("SERVICE_HANDLE=\nSERVICE_API_VERSION=v21.0\n", encoding="utf-8")
    assert policy.secret_values(layout) == {}


# ---------------------------------------------------------------------------- log redaction

def test_the_log_keeps_the_record_and_redacts_the_credential(layout):
    value = write_secret(layout)
    tasks.log(layout, "probe", "tester", note=f"responded with {value}", key=SHAPED_KEY,
              nested={"headers": [f"Authorization: Bearer {SHAPED_KEY}"]}, count=3)
    line = json.loads(layout.log.read_text(encoding="utf-8").splitlines()[-1])
    assert line["event"] == "probe" and line["actor"] == "tester" and line["count"] == 3
    assert value not in json.dumps(line) and SHAPED_KEY not in json.dumps(line)
    assert "[REDACTED]" in line["note"] and "[REDACTED]" in json.dumps(line["nested"])


def test_a_private_key_block_is_redacted_whole(layout):
    """Redaction replaces what the pattern matches, so a marker-only pattern left the entire key body and the
    END line in the log — the record was 'redacted' and still carried the key (review a4 finding 2)."""
    assert policy.find_secrets(f"loaded {PEM}", layout) == ["pattern/private_key_block"]
    assert policy.redact(f"loaded {PEM}", layout) == "loaded [REDACTED]"
    tasks.log(layout, "probe", "tester", note=f"loaded key:\n{PEM}\n")
    line = layout.log.read_text(encoding="utf-8").splitlines()[-1]
    for fragment in PEM_BODY + ["-----END RSA PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----"]:
        assert fragment not in line
    assert "[REDACTED]" in json.loads(line)["note"]


@pytest.mark.parametrize("marker", ["PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY",
                                    "ENCRYPTED PRIVATE KEY"])
def test_every_pem_flavour_is_covered(layout, marker):
    block = f"-----BEGIN {marker}-----\n{PEM_BODY[0]}\n-----END {marker}-----"
    assert policy.redact(block, layout) == "[REDACTED]"


def test_a_truncated_private_key_block_is_redacted_to_the_end_of_the_field(layout):
    """A key pasted into a tail, or cut off by a line limit, has no END line. Over-redacting the rest of one
    log field is the safe direction: a PEM marker is never incidental."""
    truncated = f"-----BEGIN RSA PRIVATE KEY-----\n{PEM_BODY[0]}\nand then the log said more"
    out = policy.redact(truncated, layout)
    assert PEM_BODY[0] not in out and out == "[REDACTED]"
    assert policy.find_secrets(truncated, layout) == ["pattern/private_key_block"]


def test_redact_leaves_ordinary_values_alone(layout):
    assert policy.redact({"path": "state/tasks/T0-25.yaml", "n": 4, "ok": True}, layout) == {
        "path": "state/tasks/T0-25.yaml", "n": 4, "ok": True}




# ---------------------------------------------------------------------------- plugin screens

def test_a_plugin_packet_screen_can_refuse_a_packet(layout, monkeypatch):
    """A project registers its own screen (content that must not leave the machine, say); it runs on every packet
    after the credential check and refuses the same way."""
    from supervisor import plugins
    reg = plugins.load(layout)

    def screen(lay, text, where):
        if "DO-NOT-SEND" in text:
            raise policy.PolicyViolation(f"{where} refused by plugin")
    reg.packet_screen(screen)
    try:
        with pytest.raises(policy.PolicyViolation, match="refused by plugin"):
            policy.assert_packet_clean("brief with DO-NOT-SEND inside", layout, "delivery packet X")
        assert policy.assert_packet_clean("ordinary brief", layout) == "ordinary brief"
    finally:
        reg.packet_screens.clear()


def test_an_adapter_supplied_prompt_is_screened_as_well(layout):
    make_task(layout, "P")
    def fn(doc):
        doc["job"] = {"adapter": "claude_cli", "prompt": f"here is the key: {SHAPED_KEY}"}
    tasks._update(layout, "P", fn)
    with pytest.raises(policy.PolicyViolation):
        REGISTRY["claude_cli"].prepare(layout, tasks.load(layout, "P"), "P.a1")
    assert not (layout.runs / "P" / "P.a1" / "prompt.md").exists()
