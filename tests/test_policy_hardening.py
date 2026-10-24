"""The policy boundary, hardened: fail-closed credential scanning, numeric credentials, key-aware redaction and
the screening of a log record's actor and event. Complements tests/test_policy.py."""
import importlib
import json
import os
import sys
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
# Base64 padding makes a bare token look like `KEY=` with an empty value.
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

def _no_network(*args, **kwargs):
    raise AssertionError("a test must never reach the network")


def test_a_configured_store_directory_that_does_not_exist_is_a_scan_failure(layout):
    """`secrets.dir` is the fourth loosening scalar in that section and it cannot be clamped —
    relocating the store is legitimate — so it is reported instead. `dir: nowhere` used to empty the literal
    screen through the deliberately quiet absent-store branch, with nothing in `secret_scan_problems()`, nothing
    in `config_problems()`: a louder loosening than any of the three knobs
    above, and silent."""
    write_policy(layout, {"version": 1, "secrets": {"dir": "nowhere"}})
    write_secret(layout)                                             # the real store, now not the configured one
    problems = policy.secret_scan_problems(layout)
    assert problems == ["nowhere/: the configured credential store (secrets.dir) does not exist, "
                        "so nothing in it could be screened"]
    with pytest.raises(policy.PolicyViolation):
        policy.assert_packet_clean("a perfectly ordinary brief", layout)


def test_the_default_store_may_still_be_absent_quietly(layout):
    """The other half: a fresh clone has no `secrets/` and must stay silent, including when the shipped config
    spells the default out (`dir: secrets`, config/policy.yaml). Only a *relocated* store is reported."""
    assert not layout.secrets.exists()
    assert policy.secret_scan_problems(layout) == []                 # no config file at all
    write_policy(layout, {"version": 1, "secrets": {"dir": "secrets"}})
    assert policy.secret_scan_problems(layout) == []

def break_listing(monkeypatch, directory, exc=PermissionError(13, "denied")):
    real = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda self: (_ for _ in ()).throw(exc) if self == directory else real(self))

def break_reads(monkeypatch, directory, exc=PermissionError(13, "denied")):
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (_ for _ in ()).throw(exc) if self.parent == directory else real(self))

def break_stat(monkeypatch, path, exc=PermissionError(13, "denied"), children=True):
    """Fault-inject at `os.stat` — underneath both `Path.stat()` and `Path.is_file()`, so the test sees the same
    thing the operating system would hand the scanner. the finding was that `is_file()` does not propagate this
    error at all, which made the `except OSError` around it dead code and a stat-denied credential file an
    absent one. `children=True` breaks the entries inside `path`; False breaks `path` itself (the config file,
    the store directory)."""
    real = os.stat

    def denied(target):
        try:
            candidate = Path(os.fsdecode(target))
        except TypeError:                        # a file descriptor: never one of ours
            return False
        return candidate.parent == path if children else candidate == path

    monkeypatch.setattr(os, "stat", lambda target, *a, **kw: (_ for _ in ()).throw(exc) if denied(target)
                        else real(target, *a, **kw))

def force_is_file(monkeypatch, directory):
    """Make `Path.is_file()` answer True for everything inside `directory`, whatever the operating system
    thinks. Paired with `break_stat` this is the regression guard: a scanner that screens
    entries with `is_file()` sees a perfectly ordinary regular file and reports nothing, so it fails the test;
    one that stats explicitly still reports the denial. It replaces asserting what `is_file()` *happens* to
    answer under the denial, which differs between interpreters (3.14/Windows never calls `os.stat`) and is
    not a property the scanner may rely on either way."""
    real = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: True if self.parent == directory else real(self))


def test_a_missing_credential_store_is_not_a_scan_failure(layout):
    """`secrets/` is git-ignored, so its absence is the ordinary state of a fresh clone: nothing to protect, and
    a working project. Only this case may be quiet."""
    assert not layout.secrets.exists()
    assert policy.secret_scan_problems(layout) == []
    make_task(layout, "T")
    packets.delivery_brief(layout, tasks.load(layout, "T"))          # dispatch still works


def test_a_credential_store_that_cannot_be_listed_refuses_every_dispatch(layout, monkeypatch):
    write_secret(layout)
    break_listing(monkeypatch, layout.secrets)
    problems = policy.secret_scan_problems(layout)
    assert problems and "cannot be listed" in problems[0] and "PermissionError" in problems[0]
    make_task(layout, "T")
    with pytest.raises(policy.PolicyViolation) as exc:
        packets.delivery_brief(layout, tasks.load(layout, "T"))      # the brief itself is clean
    assert "could not be screened" in str(exc.value) and UNSHAPED_VALUE not in str(exc.value)


def test_an_unreadable_credential_file_is_not_an_absent_one(layout, monkeypatch):
    """The reviewer's probe: a store that scans cleanly, then a read failure on a changed file. The empty result
    used to be cached as a success, after which the packet was accepted and the log kept the credential."""
    value = write_secret(layout)
    assert policy.find_secrets(f"the token is {value}", layout) == ["secrets/.env:SERVICE_TOKEN"]
    break_reads(monkeypatch, layout.secrets)
    (layout.secrets / ".env").write_text(f"SERVICE_TOKEN={value}\nEXTRA_TOKEN={value}2\n", encoding="utf-8")
    problems = policy.secret_scan_problems(layout)
    assert problems == [".env: cannot be read (PermissionError)"]
    with pytest.raises(policy.PolicyViolation):
        policy.assert_packet_clean(f"the token is {value}", layout)
    assert value not in json.dumps(policy.redact_fields({"note": f"the token is {value}"}, layout))


def test_a_stale_clean_scan_cannot_mask_a_later_listing_failure(layout, monkeypatch):
    """An empty-but-readable store and an unlistable one produce the same file stamps, so a cache keyed on the
    stamps alone would serve the earlier success forever. Enumeration therefore runs on every call and only a
    clean scan is ever served from the cache."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    assert policy.secret_scan_problems(layout) == []                 # populates the cache with a clean scan
    break_listing(monkeypatch, layout.secrets)
    assert policy.secret_scan_problems(layout) != []


def test_a_credential_file_too_large_to_scan_is_reported_rather_than_skipped(layout, monkeypatch):
    layout.secrets.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(policy, "MAX_SCAN_BYTES", 16)
    (layout.secrets / ".env").write_text(f"SERVICE_TOKEN={UNSHAPED_VALUE}\n", encoding="utf-8")
    problems = policy.secret_scan_problems(layout)
    assert problems and problems[0].startswith(".env: too large to scan")
    with pytest.raises(policy.PolicyViolation):
        policy.assert_packet_clean("a perfectly ordinary brief", layout)


def test_a_credential_file_that_is_not_utf8_is_scanned_lossily_and_reported(layout):
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / ".env").write_bytes(f"SERVICE_TOKEN={UNSHAPED_VALUE}\nNOTE=caf\xe9 latte here\n".encode("latin-1"))
    assert policy.secret_values(layout) == {".env:SERVICE_TOKEN": UNSHAPED_VALUE}      # what decoded is still screened
    assert policy.secret_scan_problems(layout) == [".env: is not valid UTF-8 and could only be scanned lossily"]


def test_a_secrets_path_that_is_not_a_directory_is_reported(layout):
    layout.secrets.write_text("someone put a file where the store belongs\n", encoding="utf-8")
    assert policy.secret_scan_problems(layout) == ["secrets exists but is not a directory"]


def test_a_credential_file_whose_stat_fails_is_reported_not_silently_skipped(layout, monkeypatch):
    """`Path.is_file()` returns False for a PermissionError rather than raising it, so the
    handler that was supposed to catch it could never run: the file was dropped from the screen, the scan was
    cached clean, the packet was accepted and the log kept the credential.

    The guard against going back to `is_file()` is `force_is_file` below rather than a probe of what
    `is_file()` answers under the denial: that answer is an interpreter and platform detail with no stable
    value to assert (it swallowed the error and said False for the probe, it says True on Windows 3.14
    where it takes a fast path that never calls `os.stat`, and it propagates the error on Windows 3.11 — a later round
    asserted the 3.14 shape and failed verification on 3.11). What is stable, and what this test pins, is that
    `stat()` makes the denial visible and that a scanner trusting `is_file()` instead would report nothing."""
    value = write_secret(layout)
    break_stat(monkeypatch, layout.secrets)
    probe = layout.secrets / ".env"
    with pytest.raises(PermissionError):
        probe.stat()                                                 # the same denial, where the scanner sees it
    force_is_file(monkeypatch, layout.secrets)                       # the most optimistic answer it can give
    problems = policy.secret_scan_problems(layout)
    assert problems == [".env: cannot be inspected (PermissionError)"]
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.assert_packet_clean(f"the token is {value}", layout)
    assert "could not be screened" in str(exc.value) and value not in str(exc.value)
    assert value not in json.dumps(policy.redact_fields({"note": f"the token is {value}"}, layout))


def test_a_truncated_credential_json_is_a_scan_failure_not_an_empty_file(layout):
    """An interrupted token refresh leaves the file without its closing brace: `json.loads`
    fails, and the text reader cannot match `{"access_token": "…"` either, so the empty result was cached as a
    success and the token was free to enter a packet and a log."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "service_token.json").write_text(f'{{"access_token": "{UNSHAPED_VALUE}"', encoding="utf-8")
    assert policy.secret_scan_problems(layout) == [
        "service_token.json: is not valid JSON and could not be fully scanned"]
    with pytest.raises(policy.PolicyViolation):
        policy.assert_packet_clean(f"the token is {UNSHAPED_VALUE}", layout)
    assert UNSHAPED_VALUE not in json.dumps(policy.redact_fields({"note": f"token {UNSHAPED_VALUE}"}, layout))


def test_a_half_written_credential_file_is_still_screened_best_effort(layout):
    """Reporting the failure is the guard; the text reader still runs over the raw text, so a value it *can*
    see is collected as well rather than being lost with the parse."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "refresh_token.json").write_text(f'{{"refresh":\n{UNSHAPED_VALUE}\n', encoding="utf-8")
    assert policy.secret_scan_problems(layout) == [
        "refresh_token.json: is not valid JSON and could not be fully scanned"]
    assert policy.secret_values(layout)["refresh_token.json:#1"] == UNSHAPED_VALUE


def test_a_well_formed_json_file_with_nothing_long_enough_is_not_a_failure(layout):
    """The boundary of the rule above: parsing succeeded, so the inspection *completed*. A file that simply
    holds nothing worth collecting must stay quiet, or every project with a short config value in secrets/ halts."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "service_token.json").write_text('{"id": "123"}', encoding="utf-8")
    assert policy.secret_scan_problems(layout) == []
    assert policy.assert_packet_clean("a perfectly ordinary brief", layout)


def test_a_directory_inside_the_credential_store_is_reported(layout):
    """A subdirectory (an editor's backup folder, a half-restored archive) is unscreened content, not an absent
    file: nobody looked inside it."""
    (layout.secrets / "archive").mkdir(parents=True, exist_ok=True)
    assert policy.secret_scan_problems(layout) == ["archive: is not a regular file and could not be screened"]
    with pytest.raises(policy.PolicyViolation):
        policy.assert_packet_clean("a perfectly ordinary brief", layout)


def test_the_packet_screen_takes_its_values_and_its_failures_from_one_scan(layout, monkeypatch):
    """The failures came from one scan and the values from a second one inside `find_secrets`,
    which dropped the second scan's failures — so a store that became unreadable between the two calls produced
    an empty value set with no problems attached and the packet was accepted. One scan per call now, and both
    halves of the judgement come from it."""
    results = [({".env:SERVICE_TOKEN": UNSHAPED_VALUE}, []),
               ({}, [".env: cannot be read (PermissionError)"])]
    calls = []

    def one_scan_then_a_failure(_layout=None):
        calls.append(1)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(policy, "_scan", one_scan_then_a_failure)
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.assert_packet_clean(f"the token is {UNSHAPED_VALUE}", layout)
    assert "secrets/.env:SERVICE_TOKEN" in str(exc.value)
    assert calls == [1]                                              # exactly one scan, not two


def test_a_failure_from_the_single_scan_still_refuses_a_clean_packet(layout, monkeypatch):
    """The other order: the one scan reports a failure, so even text with nothing in it is refused."""
    monkeypatch.setattr(policy, "_scan",
                        lambda _layout=None: ({}, [".env: cannot be inspected (PermissionError)"]))
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.assert_packet_clean("a perfectly ordinary brief", layout)
    assert "could not be screened" in str(exc.value)


def test_a_config_that_cannot_be_stat_ed_does_not_serve_a_cached_clean_scan(layout, monkeypatch):
    """The same bug class, pre-empted one layer further in: `_stamp()` answered `()` for *every* OSError, which
    is also what it answers for a file that does not exist. A stat failure on config/policy.yaml therefore
    produced a perfectly stable cache key, and the scan cached while the file was readable kept being served —
    even though that file decides which values count as credentials."""
    write_policy(layout, {"version": 1})
    write_secret(layout)
    assert policy.secret_scan_problems(layout) == []                 # populates the cache with a clean scan
    break_stat(monkeypatch, policy.config_path(layout), children=False)
    problems = policy.secret_scan_problems(layout)
    assert problems and problems[0].startswith("policy.yaml: cannot be inspected (PermissionError)")
    with pytest.raises(policy.PolicyViolation):
        policy.assert_packet_clean("a perfectly ordinary brief", layout)


def test_a_credential_store_that_cannot_be_stat_ed_is_reported(layout, monkeypatch):
    """`secrets/` itself: a stat failure on the directory is not an absent directory either."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    break_stat(monkeypatch, layout.secrets, children=False)
    problems = policy.secret_scan_problems(layout)
    assert "secrets/: cannot be inspected (PermissionError)" in problems


def test_a_log_record_is_still_written_when_the_store_cannot_be_screened(layout, monkeypatch):
    """Redacted, not refused (`tasks.log`): losing the audit record would be the worse failure. But every field
    long enough to *be* one of the credentials we failed to enumerate is withheld, and the reason is recorded."""
    write_secret(layout)
    break_listing(monkeypatch, layout.secrets)
    tasks.log(layout, "attempt_finished", "loop", note=f"finished with {UNSHAPED_VALUE}", attempts=3, ok=True)
    record = json.loads(layout.log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert UNSHAPED_VALUE not in json.dumps(record)
    assert record["note"] == "[REDACTED]"
    assert record["event"] == "attempt_finished" and record["attempts"] == 3 and record["ok"] is True
    assert any("cannot be listed" in p for p in record[policy.UNSCREENED_FIELD])


def test_a_field_too_short_to_hold_a_credential_survives_an_unscreenable_store(layout, monkeypatch):
    """`_scan` only ever collects values of `min_value_length` characters or more, so a shorter string cannot be
    one of them. That is what keeps the audit trail readable instead of a wall of placeholders."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    break_listing(monkeypatch, layout.secrets)
    out = policy.redact_fields({"status": "FAILED", "task": "T0-25", "verdict": "REVISE", "role": "engineer"}, layout)
    assert out["status"] == "FAILED" and out["task"] == "T0-25" and out["verdict"] == "REVISE"
    assert out["role"] == "[REDACTED]"                               # eight characters: long enough to be one


def test_only_a_log_records_own_field_names_are_exempt_from_the_fail_closed_blanking(layout, monkeypatch):
    """The exemption is exactly one level deep and it is about provenance, not length: a record's field names
    are `tasks.log` keyword arguments, while a key one level down can be data — a measurement keyed by the
    thing measured. Widening it would reopen the hole for every credential the scan could not enumerate."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    break_listing(monkeypatch, layout.secrets)
    out = policy.redact_fields({"measured_for": {"an-unknown-credential": 0.9}}, layout)
    assert out == {"measured_for": {"[REDACTED]": 0.9}, policy.UNSCREENED_FIELD: out[policy.UNSCREENED_FIELD]}


def test_a_credential_used_as_a_mapping_key_is_redacted(layout):
    """Keys were preserved while values were redacted, so a credential used as a nested key was written out
    verbatim beside its redacted value."""
    record = policy.redact_fields({"nested": {SHAPED_KEY: "ok"}, "plain": SHAPED_KEY}, layout)
    assert SHAPED_KEY not in json.dumps(record)
    assert record == {"nested": {"[REDACTED]": "ok"}, "plain": "[REDACTED]"}


def test_two_credential_keys_that_redact_alike_both_keep_their_record(layout):
    other = "ghp_" + "B2c3D4e5F6g7H8i9J0k1"
    out = policy.redact({SHAPED_KEY: "first", other: "second", "kept": 1}, layout)
    assert out == {"[REDACTED]": "first", "[REDACTED]#2": "second", "kept": 1}


def test_a_non_string_key_keeps_its_type(layout):
    assert policy.redact({3: "three", None: "none"}, layout) == {3: "three", None: "none"}


def test_a_credential_key_does_not_reach_the_log_file(layout):
    write_secret(layout)
    tasks.log(layout, "qa", "loop", measured={UNSHAPED_VALUE: 0.91})
    line = layout.log.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert UNSHAPED_VALUE not in line
    assert json.loads(line)["measured"] == {"[REDACTED]": 0.91}

# A digit-only password: stored as JSON text or as a JSON number, it is the same credential, and every test
# below asserts the guards treat it the same way — while unrelated numbers keep their value *and* their type.
NUMERIC_SECRET = "9384756102938475"


def write_numeric_secret(layout, quoted):
    """The reviewer's probe: a digit-only password in a credential file, stored as JSON text or as a JSON
    number. Both files say the same thing to anyone who reads them."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    body = f'{{"password": "{NUMERIC_SECRET}"}}' if quoted else f'{{"password": {NUMERIC_SECRET}}}'
    (layout.secrets / "api_credentials.json").write_text(body, encoding="utf-8")

@pytest.mark.parametrize("quoted", [True, False])

def test_a_credential_screens_the_same_whether_it_is_stored_as_text_or_as_a_number(layout, quoted):
    """`{"password": 9384756102938475}` was collected by nothing — `_pairs_from_json` dropped
    every non-string leaf — so the literal screen was empty, the scan was cached as a clean success, and the
    password was free to enter a packet in full, while the very same digits in quotes were refused."""
    write_numeric_secret(layout, quoted)
    assert policy.secret_scan_problems(layout) == []
    assert policy.secret_values(layout) == {"api_credentials.json:password": NUMERIC_SECRET}
    assert policy.find_secrets(f"log in with {NUMERIC_SECRET}", layout) == [
        "secrets/api_credentials.json:password"]
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.assert_packet_clean(f"log in with {NUMERIC_SECRET}", layout)
    assert NUMERIC_SECRET not in str(exc.value)
    name = f"N{int(quoted)}"
    make_task(layout, name)
    def fn(doc):
        doc["objective"] = f"log in with the pin {NUMERIC_SECRET}"
    tasks._update(layout, name, fn)
    with pytest.raises(policy.PolicyViolation):
        packets.delivery_brief(layout, tasks.load(layout, name))

@pytest.mark.parametrize("quoted", [True, False])

def test_a_numeric_log_value_or_nested_key_holding_a_credential_is_redacted(layout, quoted):
    """The other half: the value was collected but `_redact` returned every `int`/`float`
    unchanged and screened keys only when they were strings, so the password went into the log as a number or as
    a key beside its own redacted string form."""
    write_numeric_secret(layout, quoted)
    record = policy.redact_fields({"pin": int(NUMERIC_SECRET), "note": f"the pin is {NUMERIC_SECRET}",
                                   "measured": {int(NUMERIC_SECRET): 0.91}}, layout)
    assert NUMERIC_SECRET not in json.dumps(record)
    assert record == {"pin": "[REDACTED]", "note": "the pin is [REDACTED]",
                      "measured": {"[REDACTED]": 0.91}}


def test_a_numeric_log_field_name_holding_a_credential_is_redacted(layout):
    """A record's own field names are exempt from the fail-closed blanking, not from screening."""
    write_numeric_secret(layout, quoted=False)
    assert policy.redact_fields({int(NUMERIC_SECRET): "the pin"}, layout) == {"[REDACTED]": "the pin"}


def test_a_numeric_credential_does_not_reach_the_log_file(layout):
    write_numeric_secret(layout, quoted=False)
    tasks.log(layout, "probe", "tester", pin=int(NUMERIC_SECRET), attempts=3, ratio=0.91, ok=True)
    line = layout.log.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert NUMERIC_SECRET not in line
    record = json.loads(line)
    assert record["pin"] == "[REDACTED]"
    assert record["attempts"] == 3 and record["ratio"] == 0.91 and record["ok"] is True


def test_unrelated_numbers_keep_their_value_and_their_type(layout):
    """Counts, scores, epochs and flags are most of what the log says. `bool` is an `int` subclass, so it has to
    be excluded explicitly or `ok: true` becomes `ok: "[REDACTED]"` the moment the store holds a long number."""
    write_numeric_secret(layout, quoted=False)
    fields = {"attempts": 3, "ratio": 0.91, "ok": True, "off": False, "epoch": 1761234567890, "nothing": None,
              "nested": {7: [1, 2.5, True]}}
    assert policy.redact_fields(fields, layout) == fields


def test_a_numeric_credential_is_collected_as_the_text_the_file_was_written_with(layout):
    """Why `_scan` parses with `parse_int`/`parse_float=str`: the screen must match the characters an agent could
    reproduce, and a round trip through `float` normalises `1234567.50` to `1234567.5`, which then matches
    neither the file nor a packet quoting it."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "api_credentials.json").write_text('{"password": 1234567.50}', encoding="utf-8")
    assert policy.secret_values(layout)["api_credentials.json:password"] == "1234567.50"
    assert policy.redact("the pin is 1234567.50", layout) == "the pin is [REDACTED]"


def test_a_non_canonical_float_credential_is_collected_in_both_its_text_forms(layout):
    """The other side of the same coin. `parse_float=str` collects the file's own literal
    (`1234567.50`), but a log field holding that value is a `float`, and `_redact_number` screens it as
    `str(1234567.5)` — one character shorter, so the literal did not match and the digits were written out.
    `_scan` now parses a `.json` file twice and unions the pairs, so both forms are in the screen; the second
    one keeps its own label rather than replacing the first."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "api_credentials.json").write_text('{"password": 1234567.50}', encoding="utf-8")
    assert policy.secret_values(layout) == {"api_credentials.json:password": "1234567.50",
                                            "api_credentials.json:password (as written out)": "1234567.5"}
    assert policy.redact_fields({"pin": 1234567.50}, layout) == {"pin": "[REDACTED]"}
    assert policy.find_secrets("the pin is 1234567.5", layout) == [
        "secrets/api_credentials.json:password (as written out)"]
    with pytest.raises(policy.PolicyViolation):
        policy.assert_packet_clean("the pin is 1234567.5", layout)


def test_an_integer_credential_needs_no_second_label(layout):
    """The union must not clutter the ordinary case: an integer round-trips exactly and JSON admits no
    non-canonical integer literal, so both parses agree and there is one entry, under the real key."""
    write_numeric_secret(layout, quoted=False)
    assert policy.secret_values(layout) == {"api_credentials.json:password": NUMERIC_SECRET}


def test_a_boolean_or_null_in_a_credential_file_is_neither_a_credential_nor_a_failure(layout):
    """The only leaves left out of the collection, and the one kind that cannot hold a credential: one bit, or an
    absence. Reporting them would halt every project whose token file records `"revoked": false`."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / "service_token.json").write_text('{"revoked": false, "password": null}', encoding="utf-8")
    assert policy.secret_values(layout) == {}
    assert policy.secret_scan_problems(layout) == []
    assert policy.assert_packet_clean("a perfectly ordinary brief", layout)


def test_a_numeric_field_long_enough_to_be_a_credential_is_withheld_when_the_store_cannot_be_screened(
        layout, monkeypatch):
    """The rule a string of that length already gets, for the same reason: until the store has been enumerated, a
    credential stored as a number is indistinguishable from an id. Short numbers survive, which is what keeps the
    record readable rather than a wall of placeholders."""
    write_numeric_secret(layout, quoted=False)
    break_listing(monkeypatch, layout.secrets)
    out = policy.redact_fields({"pin": int(NUMERIC_SECRET), "attempts": 3, "ratio": 0.91}, layout)
    assert out["pin"] == "[REDACTED]" and out["attempts"] == 3 and out["ratio"] == 0.91
    assert any("cannot be listed" in p for p in out[policy.UNSCREENED_FIELD])


def test_a_credential_supplied_as_the_log_actor_does_not_reach_the_log_file(layout):
    """The blocking case: a credential as the `actor` of a log record. The field went in verbatim."""
    write_secret(layout)
    tasks.log(layout, "command", UNSHAPED_VALUE, kind="approve", id="T0-25")
    line = layout.log.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert UNSHAPED_VALUE not in line
    record = json.loads(line)
    assert record["actor"] == "[REDACTED]" and record["event"] == "command"


def test_a_credential_supplied_as_the_log_event_does_not_reach_the_log_file(layout):
    """`event` sits beside it and is built the same way; a caller that passes a value through is the same hole."""
    write_numeric_secret(layout, quoted=False)
    tasks.log(layout, f"event-{NUMERIC_SECRET}", "loop")
    line = layout.log.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert NUMERIC_SECRET not in line
    assert json.loads(line)["event"] == "event-[REDACTED]"


def test_the_actor_is_screened_but_not_blanked_while_the_store_cannot_be_screened(layout, monkeypatch):
    """Screened on the *non-blanking* path, deliberately. These two are what an audit record is *about*, so the
    fail-closed blanking that withholds every long field value must not apply to them: a log full of
    `[REDACTED] [REDACTED]` lines would say nothing at the one moment the owner needs to read it. Same
    exemption, and same cost, as a record's own field names."""
    write_secret(layout)
    break_listing(monkeypatch, layout.secrets)
    assert policy.secret_scan_problems(layout) != []
    tasks.log(layout, "attempt_finished", "chief_architect", note=f"finished with {UNSHAPED_VALUE}")
    record = json.loads(layout.log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["actor"] == "chief_architect" and record["event"] == "attempt_finished"
    assert record["note"] == "[REDACTED]"                            # the field *is* blanked


def test_a_shaped_key_as_the_actor_is_screened_even_without_a_store(layout):
    """The pattern screen reaches `actor` too, so a store this process never managed to read is not the only
    thing standing between a key-shaped string and the log."""
    assert policy.screen_log_identity(SHAPED_KEY, layout) == "[REDACTED]"
    tasks.log(layout, "probe", f"agent {SHAPED_KEY}")
    line = layout.log.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert SHAPED_KEY not in line and json.loads(line)["actor"] == "agent [REDACTED]"


