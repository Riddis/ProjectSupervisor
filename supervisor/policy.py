"""The policy boundary: credential isolation. `config/policy.yaml` is the data; this module is the only mechanism.

Three guards, each at the one boundary the thing it protects has to cross:

* **packets** - `supervisor.packets` passes every brief through `assert_packet_clean()`, so a dispatch whose prompt
  carries a credential (a literal value from `secrets/`, a secret-shaped string, or a secret-suffixed
  environment value) is refused before any process is started. Credentials reach an agent child through its
  subscription login only, never through text (`adapters.process.scrub_env` strips the environment).
* **logs** - `supervisor.tasks.log()` is the single append path to `state/log.jsonl`, and every part of a record
  that does not come from this machine's own clock is screened there. Enumerated, because "every field" is the
  claim and it has to be true: the keyword fields and everything nested inside them (values *and* mapping keys)
  go through `redact_fields()`, with fail-closed blanking while the store is unreadable; the record's own field
  names, its `actor` and its `event` go through the non-blanking `_screen_name()` path
  (`screen_log_identity()`) - `actor` because it is *data*, fed from a queued `state/commands/*.json` file, not
  an identifier in source code. The `ts` stamp is `store.now_iso()` and is the one value a caller cannot
  influence.
* **plugin screens** - a project may register further packet screens (`registry.packet_screen`) for content
  that must not leave the machine; `assert_packet_clean()` runs them after the credential check.

Everything fails closed. The defaults below apply when `config/policy.yaml` is missing or unreadable.

Where a configured value is a list of *things to refuse or strip* - secret patterns, secret key words, secret
environment suffixes - it is **unioned** with the built-in floor: a config edit there can only tighten, and
removing a built-in needs a code change in `DEFAULTS`. The three scalars in the secrets section whose natural
direction is *loosening* are **clamped** rather than trusted - `min_value_length` can only be lowered,
`ignore_files` may never hide a dotenv file, and an empty `redaction_placeholder` falls back to the built-in
(`_clamped_secrets()`). The fourth, `secrets.dir`, cannot be clamped - relocating the store is legitimate - so it
is **reported** instead: only the default `secrets/` may be absent quietly, and a configured directory that does
not exist is a scan failure, which is what stops `dir: nowhere` from emptying the literal screen in silence.

All of that assumes the config file at least has the right *shapes*. It may not: `secrets: {patterns: null}` is
valid YAML that would replace a built-in list with `None` and leave the guard with nothing to match. So a
configured entry may only replace a floor entry **of the same kind** (`_kind`); a mismatch is dropped, the floor
stands, and the mismatch is reported by `config_problems()` (shown by `supervisor doctor`).

The same reasoning applies one level down, to the guards' *inputs*, the credential store above all. An `OSError`
from `secrets/` must not leave the literal screen empty or partial with that result cached as a successful scan,
or a credential nobody could enumerate is a credential nobody screened for. `_scan()` separates *there is no
store* (absent `secrets/`, the normal state of a fresh clone) from *the store could not be read*, returns the
failures beside the values, and never caches a failed scan; `secret_scan_problems()` exposes them, dispatch is
refused while any of them stands, and the log redactor withholds every field *value* long enough to *be* a
credential rather than writing it unscreened (the exemptions are listed under **logs** above: field names,
`actor` and `event`, which are screened but never blanked). Nothing in `secrets/` is excluded from the screen
except by a **completed, successful inspection** (or the owner's `ignore_files`), and the only credential store
that may be absent quietly is the **default** one, not a relocated `secrets.dir`.

Redaction screens mapping **keys** as well as values: a credential used as a key in a nested log field would
otherwise be written out verbatim beside its redacted value.

And one level down again, to the credential's **storage form**. A digit-only password stored as a JSON number
is the same credential as its quoted form, and it reaches a packet, a log value or a log key as the same digits
either way. A credential is the text it is written with, whatever type holds it - `_number_text()` is that one
rule, `_pairs_from_json()` collects numbers through it, `_redact_number()` screens them through it, and
unrelated numbers keep their value and their type. One number can be *written* two ways, so a `.json` file is
parsed twice and the pairs unioned: the source literal (`1234567.50`, via `parse_int`/`parse_float=str`) and the
canonical `str()` form the redactor sees when that same number arrives as a log field (`1234567.5`).
"""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from .paths import Layout

CONFIG_NAME = "policy.yaml"

# Built-in floor. Anything a config file says is merged on top of this; the lists are unioned, not replaced.
DEFAULTS: dict = {
    "secrets": {
        "dir": "secrets",
        "ignore_files": ["README.md"],
        "min_value_length": 8,
        # The single source of truth for "this variable name names a credential": `adapters.process.scrub_env`
        # strips the child environment with exactly this list, `env_secret_values()` screens this process's own
        # environment with it, and a `KEY=VALUE` line in `secrets/` is only collected as a literal when its key
        # matches it (or `secret_key_words`, or a shape pattern below).
        "env_key_suffixes": ["_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_SECRET", "_TOKEN", "_PASSWORD"],
        # Substrings that make a key (or a credential file's name) secret-like even without a matching suffix.
        "secret_key_words": ["API_KEY", "SECRET", "TOKEN", "PASSWORD", "PASSPHRASE", "CREDENTIAL", "PRIVATE_KEY",
                             "SESSION_KEY", "SIGNING_KEY", "REFRESH"],
        "redaction_placeholder": "[REDACTED]",
        "patterns": [
            {"name": "anthropic_api_key", "regex": r"sk-ant-[A-Za-z0-9_\-]{16,}"},
            {"name": "openai_api_key", "regex": r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{24,}"},
            {"name": "github_token", "regex": r"gh[pousr]_[A-Za-z0-9]{20,}"},
            {"name": "slack_token", "regex": r"xox[abpsr]-[A-Za-z0-9\-]{12,}"},
            {"name": "aws_access_key_id", "regex": r"AKIA[0-9A-Z]{16}"},
            {"name": "google_api_key", "regex": r"AIza[0-9A-Za-z_\-]{35}"},
            {"name": "meta_graph_token", "regex": r"EAA[A-Za-z0-9]{24,}"},
            {"name": "jwt", "regex": r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"},
            # The whole block, not just the marker: `redact()` substitutes what the pattern matches, so matching
            # the BEGIN line alone left the base64 body and the END line in the log. A block with no END line
            # (truncated output, a key pasted into a tail) is redacted to the end of the string - over-redacting
            # a log field is the safe direction, and a PEM marker is never incidental.
            {"name": "private_key_block",
             "regex": r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"},
            {"name": "bearer_header", "regex": r"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._\-]{16,}"},
            {"name": "assigned_secret",
             "regex": r"(?i)\b[A-Za-z0-9_]*(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|CLIENT_SECRET|SECRET_KEY|PASSWORD)"
                      r"\b\s*[:=]\s*[\"']?[A-Za-z0-9/+_.\-]{16,}"},
        ],
    },
}


class PolicyError(RuntimeError):
    """Base: a policy guard refused something. Never carries the offending value."""


class PolicyViolation(PolicyError):
    """A credential, or content that may not leave the machine, reached a boundary that forbids it."""


# ---------------------------------------------------------------------------- loading

_doc_cache: dict[Path, tuple[tuple, dict, list[str]]] = {}


def _layout(layout: Layout | None) -> Layout:
    return layout if layout is not None else Layout()


def config_path(layout: Layout | None = None) -> Path:
    return _layout(layout).config / CONFIG_NAME


def _stamp(path: Path) -> tuple[tuple, str]:
    """`(cache stamp, inspection failure)` for one path.

    A stamp of `()` means **this path does not exist**, and nothing else. The first draft answered `()` for every
    `OSError`, so a path that could not be stat-ed was indistinguishable from an absent one *and* produced a
    perfectly stable cache key: a scan cached while `config/policy.yaml` was readable kept being served after
    the file stopped being readable, even though that file decides which values count as credentials (the same bug class
    as a cached-but-failed secrets scan). `FileNotFoundError` stays quiet - neither the config nor
    `secrets/` has to exist - and every other `OSError` is returned as a failure for the caller to report, never
    cached."""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size), ""
    except FileNotFoundError:
        return (), ""
    except OSError as exc:
        return (), type(exc).__name__


def _names(values: Iterable | None, lower: bool = False) -> list[str]:
    """Non-empty strings from a configured list of names, normalised. Strings only: a stray `null` or nested
    value in the data must contribute nothing rather than a junk name like `NONE` - and an empty string in
    `env_key_suffixes` would make `str.endswith` match every variable there is."""
    out = []
    for item in values or []:
        if isinstance(item, str) and item.strip():
            out.append(item.strip().lower() if lower else item.strip().upper())
    return out


def _union(base: Iterable, extra: Iterable) -> list:
    out = list(base)
    for item in extra:
        if item not in out:
            out.append(item)
    return out


def _kind(value: Any) -> str:
    """The merge kind of a value. A configured entry may only replace a floor entry of the same kind, so
    `secrets: {patterns: null}` cannot stand in for the list it looks like."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "mapping"
    if isinstance(value, list):
        return "list"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _mismatch(where: str, floor: Any, item: Any) -> str:
    return (f"{where}: expected {_kind(floor)}, got {_kind(item)} "
            f"({'null' if item is None else type(item).__name__}) - built-in floor kept")


def _merge(defaults: dict, configured: dict) -> tuple[dict, list[str]]:
    """Section-wise merge. Scalars from the config win, lists are unioned with the built-in floor - but only
    where the configured value has the same kind as the floor value it would replace.

    Returns `(effective policy, rejected entries)`. A wrong-kinded entry is *dropped*: it is the one class of
    config error that can silently delete a protection (a null list leaves a guard with nothing to match, a
    string where a flag belongs is truthy), and a security floor that a typo can remove is not a floor. Keys
    with no entry in `DEFAULTS` (`version`, a project's own sections, …) have no floor to
    protect and are taken as given (a project plugin may read them through `section()`)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in defaults.items()}
    problems: list[str] = []
    for name, value in (configured or {}).items():
        if name not in defaults:
            out[name] = value
            continue
        floor = defaults[name]
        if _kind(value) != _kind(floor):
            problems.append(_mismatch(name, floor, value))
            continue
        if not isinstance(floor, dict):
            out[name] = value
            continue
        merged = out[name]
        for key, item in value.items():
            if key in floor and _kind(item) != _kind(floor[key]):
                problems.append(_mismatch(f"{name}.{key}", floor[key], item))
                continue
            if isinstance(item, list) and isinstance(merged.get(key), list):
                merged[key] = _union(merged[key], item)
            else:
                merged[key] = item
    return out, problems


def _loaded(layout: Layout | None = None) -> tuple[dict, list[str]]:
    """The effective policy and the configured entries that were rejected, cached on the config's mtime/size."""
    path = config_path(layout)
    stamp, failure = _stamp(path)
    cached = _doc_cache.get(path)
    if cached and not failure and cached[0] == stamp:
        return cached[1], cached[2]
    configured: dict = {}
    problems: list[str] = []
    if failure:
        # The file may still read even though it cannot be stat-ed, so the read below is still attempted; but the
        # owner is told, and nothing is cached under a stamp that cannot be trusted.
        problems.append(f"{CONFIG_NAME}: cannot be inspected ({failure}) - built-in floor kept")
    if stamp or failure:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                configured = loaded
            elif loaded is not None:
                problems.append(f"{CONFIG_NAME}: top level is {_kind(loaded)}, not a mapping - built-in floor kept")
        except (OSError, yaml.YAMLError) as exc:
            problems.append(f"{CONFIG_NAME}: unreadable or malformed YAML ({type(exc).__name__}) - "
                            "built-in floor kept")
    doc, rejected = _merge(DEFAULTS, configured)
    problems += rejected
    if not failure:
        _doc_cache[path] = (stamp, doc, problems)
    return doc, problems


def load(layout: Layout | None = None) -> dict:
    """The effective policy: built-in floor merged with `config/policy.yaml`. An unreadable, malformed or
    wrong-shaped config leaves the floor in place rather than opening anything up."""
    return _loaded(layout)[0]


def config_problems(layout: Layout | None = None) -> list[str]:
    """Human-readable descriptions of every `config/policy.yaml` entry that was rejected (unparseable file,
    wrong kind for the floor entry it would replace). Each one means a value the owner wrote is *not* in
    effect; `supervisor doctor` reports them."""
    return list(_loaded(layout)[1])


def section(name: str, layout: Layout | None = None) -> dict:
    value = load(layout).get(name)
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------- credentials

_DOTENV = re.compile(r"(?i)(^|\.)env(\.|$)")      # `.env`, `.env.local`, `prod.env` - the files that hold keys


def _clamped_secrets(layout: Layout | None = None) -> dict:
    """The secrets section with the three loosenable scalars clamped. `_merge` lets a config scalar win and
    unions lists, which is the right default everywhere except here: this section's knobs all loosen when they
    grow, so each one is bounded by the built-in floor rather than replaced by it."""
    cfg = dict(section("secrets", layout))
    floor = DEFAULTS["secrets"]
    try:                                           # may only be *lowered*: a huge value would disable the check
        minimum = min(int(cfg.get("min_value_length", floor["min_value_length"])), int(floor["min_value_length"]))
    except (TypeError, ValueError):
        minimum = int(floor["min_value_length"])
    cfg["min_value_length"] = max(1, minimum)
    # `ignore_files` is a list, so the union only ever grows it - and growth here *hides* a file from the literal
    # scanner. A dotenv file may never be hidden; everything else is the owner's call.
    cfg["ignore_files"] = [str(n) for n in (cfg.get("ignore_files") or []) if not _DOTENV.search(str(n))]
    mark = str(cfg.get("redaction_placeholder") or "").strip()
    cfg["redaction_placeholder"] = str(cfg.get("redaction_placeholder")) if mark else floor["redaction_placeholder"]
    return cfg


def secrets_dir(layout: Layout | None = None) -> Path:
    lay = _layout(layout)
    return lay.root / str(section("secrets", lay).get("dir") or "secrets")


def placeholder(layout: Layout | None = None) -> str:
    return str(_clamped_secrets(layout)["redaction_placeholder"])


def env_key_suffixes(layout: Layout | None = None) -> tuple[str, ...]:
    """Variable-name suffixes that name a credential: the floor unioned with the configured list. Growth only
    tightens (one more variable stripped from a child, one more value screened), so the union is safe here.
    `adapters.process.scrub_env` reads this, so the data file and the mechanism cannot drift apart."""
    configured = _names(section("secrets", layout).get("env_key_suffixes"))
    return tuple(_union(_names(DEFAULTS["secrets"]["env_key_suffixes"]), configured))


def secret_key_words(layout: Layout | None = None) -> tuple[str, ...]:
    configured = _names(section("secrets", layout).get("secret_key_words"))
    return tuple(_union(_names(DEFAULTS["secrets"]["secret_key_words"]), configured))


def _looks_secret(name: str, layout: Layout | None = None) -> bool:
    """True when a key, JSON path or file name *names* a credential. Normalised so `token.access`,
    `service-token.json` and `ACCESS_TOKEN` are all recognised."""
    norm = re.sub(r"[^A-Z0-9]+", "_", str(name).upper()).strip("_")
    if not norm:
        return False
    return (norm.endswith(env_key_suffixes(layout))
            or any(word in norm for word in secret_key_words(layout)))


_pattern_cache: dict[tuple, list[tuple[str, re.Pattern]]] = {}


def secret_patterns(layout: Layout | None = None) -> list[tuple[str, re.Pattern]]:
    """(name, compiled regex) pairs: the built-in shapes plus any configured ones. An invalid configured regex
    is skipped - one bad edit must not disable the whole guard.

    The floor is unioned in *here* as well as in `_merge`, so that this accessor - the thing the packet screen
    and the log redactor actually call - can never return fewer patterns than `DEFAULTS` holds, whatever the
    config file says or fails to say."""
    specs = [s for s in (section("secrets", layout).get("patterns") or []) if isinstance(s, dict)]
    key = tuple(_union([(str(s["name"]), str(s["regex"])) for s in DEFAULTS["secrets"]["patterns"]],
                       [(str(s.get("name")), str(s.get("regex"))) for s in specs]))
    cached = _pattern_cache.get(key)
    if cached is not None:
        return cached
    compiled: list[tuple[str, re.Pattern]] = []
    for name, regex in key:
        try:
            compiled.append((name, re.compile(regex)))
        except re.error:
            continue
    _pattern_cache[key] = compiled
    return compiled


def env_secret_values(layout: Layout | None = None) -> dict[str, str]:
    """Secret-suffixed environment values of *this* process, keyed by variable name. They must never appear in
    a packet or a log line even though an agent child never receives them (`process.scrub_env`)."""
    suffixes = env_key_suffixes(layout)
    minimum = int(_clamped_secrets(layout)["min_value_length"])
    return {k: v for k, v in os.environ.items()
            if k.upper().endswith(suffixes) and isinstance(v, str) and len(v) >= minimum}


_ASSIGNMENT = re.compile(r"^(?:export\s+)?[A-Za-z_][A-Za-z0-9_.\-]*\s*=")


def _pairs_from_text(text: str, minimum: int) -> list[tuple[str, str]]:
    """(label, value) for a `.env`-style or bare-token file: `KEY=VALUE` lines labelled by their key, a
    single-token line labelled by its position. Comments and prose lines are ignored.

    A line only counts as an assignment when it is *shaped* like one and carries a long enough value after the
    `=`; otherwise the whole line is tried as a bare token. Base64 padding is why: `…QUJD==` is a credential,
    but splitting on the first `=` turns it into a plausible key with an empty value, so a token file whose one
    line is padded used to yield nothing at all."""
    out: list[tuple[str, str]] = []
    for index, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _ASSIGNMENT.match(line):
            key, _, value = line.partition("=")
            key, value = key.strip().removeprefix("export").strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if len(value) >= minimum:
                out.append((key or f"#{index}", value))
                continue
        if len(line) >= minimum and not re.search(r"\s", line):
            out.append((f"#{index}", line))         # a bare token: the line itself is the value
    return out


def _number_text(value: Any) -> str | None:
    """The text form of a numeric scalar, or `None` for anything that is not one.

    A credential's *storage form* must not decide whether it is screened. `{"password": 9384756102938475}` and
    `{"password": "9384756102938475"}` are the same credential, and it reaches a packet, a log value and a log
    key as the same digits either way. `bool` is excluded deliberately - it is an `int` subclass,
    and `True`/`False` hold one bit, never a credential - which is also what keeps `ok: true` in the log.

    One function, used by both halves: the scanner collects numeric values with it and the redactor screens
    numeric values and keys with it, so the two cannot drift into disagreeing about which *types* are numbers,
    or about how a number is rendered.

    What it does **not** do is make the two text forms of one number the same. `str()` normalises, and a JSON
    literal need not be canonical: `1234567.50` in a credential file and `str(1234567.5)` for the same value in
    a log field differ by a character, so collecting only the literal left a float credential collected and
    still writable. `_scan` therefore collects a JSON number in **both** forms and the
    screen matches whichever one an agent reproduces. Integers need no such care - they round-trip exactly, and
    JSON admits no non-canonical integer literal."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return str(value)


def _pairs_from_json(data: Any, minimum: int, prefix: str = "") -> list[tuple[str, str]]:
    """`(JSON path, value as text)` for every scalar in a parsed credential file.

    Numbers count. They used to be dropped along with every other non-string leaf, so a digit-only password
    stored as a JSON number was collected by nothing, screened by nothing, and free to enter a packet in full
    while the same password in quotes was refused. The text seen here is whatever the caller's
    parse produced: `_scan` calls this twice per `.json` file and unions the results, once on a parse with
    `parse_int=str, parse_float=str` (so the text is the file's own literal, `1.50`) and once on a plain parse
    (so the text is `_number_text`'s canonical `1.5`, which is what the log redactor screens a float field
    with). Neither form alone is enough: the first is what an agent quoting the file would reproduce, the second
    is what `json.dumps` would write for the same value. `true`, `false` and `null` are the only leaves left
    out, and they are the one kind that cannot carry a credential."""
    if isinstance(data, str):
        return [(prefix or "#0", data)] if len(data) >= minimum else []
    if isinstance(data, dict):
        return [p for k, v in data.items() for p in _pairs_from_json(v, minimum, f"{prefix}.{k}" if prefix else str(k))]
    if isinstance(data, list):
        return [p for i, v in enumerate(data) for p in _pairs_from_json(v, minimum, f"{prefix}[{i}]")]
    text = _number_text(data)
    if text is not None:
        return [(prefix or "#0", text)] if len(text) >= minimum else []
    return []


_values_cache: dict[Path, tuple[tuple, dict[str, str]]] = {}
MAX_SCAN_BYTES = 1_000_000


def _is_literal_secret(file_name: str, label: str, value: str, layout: Layout | None = None) -> bool:
    """Whether a `(label, value)` pair collected from `secrets/` is a credential.

    A real `.env` holds non-secret config beside the keys - an account id, a handle, a base URL - and this
    check *refuses* packets, so "every value of 8+ characters" would make any brief that mentions the account id
    undispatchable. A pair counts when at least one of these holds:

    * the value matches a shape pattern (it is a credential wherever it sits);
    * the key, or its JSON path, names a credential (`*_TOKEN`, `client_secret`, `token.access`);
    * the file itself is a dedicated credential file (`service_token.json`), in which case every value in it
      counts - that is what the file is for.

    Non-secret configuration therefore belongs in `config/`, and a non-secret value in `secrets/.env` is only
    scanned if it is named like a credential (`docs/runbooks/policy.md` §2).

    How the value was *stored* is not part of this judgement: a JSON number is offered here as the text it is
    written with (`_pairs_from_json`), so a numeric password under a credential-named key counts - and so does a
    numeric expiry inside a dedicated credential file, which is the same over-screening the whole-file rule
    above already chose, in the same direction."""
    if any(pattern.search(value) for _name, pattern in secret_patterns(layout)):
        return True
    if _looks_secret(file_name, layout):
        return True
    return not label.startswith("#") and _looks_secret(label, layout)


def _scan(layout: Layout | None = None) -> tuple[dict[str, str], list[str]]:
    """`(literal credential values, scan failures)` for `secrets/`.

    Values are keyed by `<file>:<key>` label - the label is logged, the value never is - and the successful
    scan is cached on the directory's file stamps, so a key written a moment ago is seen at once. This is the
    strongest of the three credential checks: a value named like a credential is caught whether or not it looks
    like one (`_is_literal_secret` for what "named like" means).

    The two outcomes a reader must never conflate are *there is no credential store* and *the store could not
    be read*. The **default** `secrets/` is git-ignored, so its absence is the ordinary state of a fresh clone
    and means there is nothing to protect; a store the config *relocated* (`secrets.dir`) and that does not
    exist is reported instead, because there the absence is a config edit rather than a fresh clone. An
    unreadable directory, an unreadable file, a file too large to scan or one that is
    not UTF-8 means something is in there that this function did not look at - and a credential it did not
    enumerate is one it cannot screen for. That used to be indistinguishable from the empty case: a single
    `OSError` produced an empty or partial result which was then cached as a success, after which packets
    carrying the affected credential were accepted and logs kept it. Failures are now
    returned beside the values, a failed scan is never cached (so a transient error clears itself), and
    enumeration is re-run on every call rather than being short-circuited by a cache entry written while the
    directory was still readable.

    The default is **inverted**: an entry in `secrets/` is dropped from the screen only when a completed,
    successful inspection shows there is nothing in it to collect - or when the owner's `ignore_files` names it.
    Every other outcome appends a problem. Allow-listing only the failure modes already known leaves the one
    nobody has broken yet: `Path.is_file()` cannot be trusted to raise a `PermissionError`
    (it returns False on some interpreters, True on Windows 3.14, and raises on Windows 3.11), which makes an
    `except OSError` around it dead code where it matters and a stat-denied credential file a silently skipped
    one, and a `.json` file left half-written by an interrupted token refresh fails `json.loads`, matches
    nothing in the text reader either, and would be cached as a clean empty scan. Inspection is therefore
    explicit here - one `stat()` per entry, checked with `stat.S_ISREG` and
    reused for the size check - and any `OSError`, any non-regular entry and any unparseable JSON is reported."""
    directory = secrets_dir(layout)
    cfg = _clamped_secrets(layout)
    ignore = {str(n) for n in (cfg.get("ignore_files") or [])}
    minimum = int(cfg["min_value_length"])
    problems: list[str] = []
    # The config takes part in the stamp: which pairs count depends on the key words and patterns, so an edit to
    # config/policy.yaml must invalidate this cache as surely as a new file in secrets/ does - and a config this
    # process cannot stat is one whose contents it cannot vouch for, which is a scan failure of its own.
    config_stamp, config_failure = _stamp(config_path(layout))
    if config_failure:
        problems.append(f"{CONFIG_NAME}: cannot be inspected ({config_failure}), so which values in "
                        f"{directory.name}/ count as credentials is unknown")
    dir_stamp, dir_failure = _stamp(directory)
    if dir_failure:
        problems.append(f"{directory.name}/: cannot be inspected ({dir_failure})")
    entries: list[Path] = []
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        # No store at all. Quiet for the *default* location only: `secrets/` is git-ignored, so its absence is
        # the ordinary state of a fresh clone. A `secrets: {dir: …}` pointing somewhere that does not exist is a
        # different thing - it empties the literal screen on the owner's say-so while nothing reports it, which
        # would make `dir` a louder loosening knob than the three `_clamped_secrets` exists to bound
        #. `dir` cannot be clamped the way those are - relocating the store is
        # legitimate - so it is *reported* instead.
        if directory != _layout(layout).root / DEFAULTS["secrets"]["dir"]:
            problems.append(f"{directory.name}/: the configured credential store (secrets.dir) does not "
                            f"exist, so nothing in it could be screened")
    except NotADirectoryError:
        problems.append(f"{directory.name} exists but is not a directory")
    except OSError as exc:
        problems.append(f"{directory.name}/ cannot be listed ({type(exc).__name__})")
    files: list[tuple[Path, os.stat_result]] = []
    for path in entries:
        if path.name in ignore:
            continue                            # excluded by the owner's own config, not by a failed inspection
        try:
            st = path.stat()
        except OSError as exc:
            # Every OSError is a failure here, `FileNotFoundError` included: `iterdir` returned this entry a
            # moment ago, so a target that is gone means a dangling symlink or a file removed mid-scan -
            # unscreened content either way, unlike the absent directory above.
            problems.append(f"{path.name}: cannot be inspected ({type(exc).__name__})")
            continue
        if not stat.S_ISREG(st.st_mode):
            # A subdirectory, a socket, or a symlink that does not resolve to a file. Not an absent file: nobody
            # looked inside it, and `secrets/` holds flat KEY=VALUE text and JSON, nothing else.
            problems.append(f"{path.name}: is not a regular file and could not be screened")
            continue
        files.append((path, st))
    # Only a clean scan is served from the cache or written to it - an enumeration failure always falls through
    # to a fresh read, so a success recorded while the store was readable cannot be served after it stops being.
    stamp = (config_stamp, dir_stamp, tuple((p.name, s.st_mtime_ns, s.st_size) for p, s in files))
    cached = _values_cache.get(directory)
    if not problems and cached and cached[0] == stamp:
        return cached[1], []
    found: dict[str, str] = {}
    for path, st in files:
        size = st.st_size                       # the stat taken above: one inspection per file, not one per check
        if size > MAX_SCAN_BYTES:
            # Skipping it silently is the same hole as failing to read it: nothing in secrets/ should be this
            # big, and whatever is in there has not been screened.
            problems.append(f"{path.name}: too large to scan ({size} bytes)")
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            problems.append(f"{path.name}: cannot be read ({type(exc).__name__})")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Scan what decodes, and say so: a file read lossily yields values that no longer match the bytes
            # an agent could reproduce, so the screen built from it is incomplete.
            text = raw.decode("utf-8", errors="replace")
            problems.append(f"{path.name}: is not valid UTF-8 and could only be scanned lossily")
        pairs: list[tuple[str, str]] = []
        if path.suffix.lower() == ".json":
            try:
                # Parsed twice, pairs unioned, because one number has two text forms and both are the
                # credential: `parse_int`/`parse_float=str` yields the source text it was written with (what an
                # agent quoting the file would reproduce), the plain parse yields `_number_text`'s canonical
                # `str()` form (what the log redactor screens a numeric field with). `1234567.50` collected
                # alone did not match the `1234567.5` of the same value in a log field.
                pairs = _pairs_from_json(json.loads(text, parse_int=str, parse_float=str), minimum)
                seen = set(pairs)
                pairs += [p for p in _pairs_from_json(json.loads(text), minimum) if p not in seen]
            except (ValueError, RecursionError):
                # A half-written credential file is a *scan failure*, not an empty one. An interrupted token
                # refresh leaves `{"access_token": "<value>"` with no closing brace: `json.loads` raises, and
                # the text reader below cannot match that line either (it is not shaped like an assignment and
                # it contains spaces), so the empty result used to be cached as a success and the token was
                # then free to enter a packet and a log. Whatever the first parse did
                # collect is *kept* rather than thrown away: only the second parse can fail on its own (an
                # integer literal longer than `int()` will convert), and in that case the literal forms above
                # are still a screen - an incomplete one, which is what the problem says.
                problems.append(f"{path.name}: is not valid JSON and could not be fully scanned")
        if not pairs:
            # Also the best-effort screen for the malformed case above: a file that parses but holds no long
            # enough value falls through here unremarked, exactly as before.
            pairs = _pairs_from_text(text, minimum)
        for key, value in pairs:
            if not _is_literal_secret(path.name, key, value, layout):
                continue
            label = f"{path.name}:{key}"
            if label in found and found[label] != value:
                # One JSON number, two text forms (the file's literal and the canonical `str()` the redactor
                # screens with): both have to be screened, so the second does not replace the first. The
                # secrecy judgement above is always made on the *real* key, never on this derived label.
                label = f"{label} (as written out)"
            found[label] = value
    if not problems:
        _values_cache[directory] = (stamp, found)
    return found, problems


def secret_values(layout: Layout | None = None) -> dict[str, str]:
    """Literal credential values currently in `secrets/` (`_scan`). Callers that enforce anything must consult
    `secret_scan_problems()` too: this dict is "what was found", not "what is there"."""
    return _scan(layout)[0]


def secret_scan_problems(layout: Layout | None = None) -> list[str]:
    """Human-readable reasons the credential store could not be screened completely (`_scan`). Non-empty means
    the literal check is incomplete, so `assert_packet_clean()` refuses every dispatch, `redact_fields()`
    withholds any log field long enough to be a credential. Names files, never their contents."""
    return _scan(layout)[1]


def _find_in(text: str, values: dict[str, str], layout: Layout | None = None) -> list[str]:
    """Labels of every credential in `text`, screened against literal values that have **already been scanned**.

    Split out of `find_secrets()` so that one caller can bind the values and the failures of a *single* scan.
    `assert_packet_clean()` used to read the failures from one `_scan()` and the values from a second one inside
    `find_secrets()`, which dropped the second scan's failures: a store that became unreadable between the two
    calls - a concurrent credential write, a permissions change, an antivirus lock - produced an empty value set
    with no problems attached, and the packet was accepted."""
    if not text:
        return []
    hits: list[str] = []
    for label, value in values.items():
        if value and value in text:
            hits.append(f"secrets/{label}")
    for name, value in env_secret_values(layout).items():
        if value in text:
            hits.append(f"env/{name}")
    for name, pattern in secret_patterns(layout):
        if pattern.search(text):
            hits.append(f"pattern/{name}")
    seen, out = set(), []
    for hit in hits:
        if hit not in seen:
            seen.add(hit)
            out.append(hit)
    return out


def find_secrets(text: str, layout: Layout | None = None) -> list[str]:
    """Labels of every credential found in `text` - pattern names and `secrets/` / environment labels. Never
    returns the matched value: the whole point is that it stays out of messages, exceptions and logs.

    Callers that *enforce* something must consult `secret_scan_problems()` as well, or use the one-scan path in
    `assert_packet_clean()`: like `secret_values()`, this answers "what was found", not "what is there"."""
    return _find_in(text, _scan(layout)[0], layout)


UNSCREENED_FIELD = "policy_unscreened"


def _redaction(layout: Layout | None = None):
    """Everything one redaction pass needs, computed once: the placeholder, the literal values, the compiled
    patterns, whether the credential store could be screened at all, and the length below which a string cannot
    contain a collected literal. Hoisted out of the recursion because a log record has many fields and `_scan`
    stats `secrets/` on every call."""
    cfg = _clamped_secrets(layout)
    values, problems = _scan(layout)
    literals = [v for v in list(values.values()) + list(env_secret_values(layout).values()) if v]
    return (str(cfg["redaction_placeholder"]), literals, secret_patterns(layout), problems,
            max(1, int(cfg["min_value_length"])))


def _screen(text: str, ctx: tuple) -> str:
    """Exact-match screening of one string: every known literal value and every pattern match replaced by the
    placeholder. This is the whole of redaction when the credential store could be read."""
    mark, literals, patterns, _problems, _minimum = ctx
    for literal in literals:
        if literal in text:
            text = text.replace(literal, mark)
    for _name, pattern in patterns:
        text = pattern.sub(mark, text)
    return text


def _place(out: dict, key: Any, value: Any) -> None:
    """Add `key` to `out`, disambiguating a collision rather than dropping the entry. Two different
    credential-bearing keys redact to the same placeholder; losing one of them would silently lose an audit
    record, so the second becomes `[REDACTED]#2` and the collision stays visible."""
    if key in out:
        suffix = 2
        while f"{key}#{suffix}" in out:
            suffix += 1
        key = f"{key}#{suffix}"
    out[key] = value


def _redact_number(value: int | float, text: str, ctx: tuple) -> Any:
    """One numeric scalar, screened as the digits it is. Returns the number unchanged unless those digits are
    (or, while the store is unreadable, could be) a credential, in which case the whole field becomes the
    placeholder.

    Unrelated numbers must survive with their type, or every count, score and duration in the log turns into a
    string: `4`, `0.9` and a 10-digit id are returned exactly as they came in. The placeholder replaces the
    field entire rather than the matched span - a half-redacted number is neither a usable value nor a safe one."""
    if _screen(text, ctx) != text:
        return ctx[0]
    if ctx[3] and len(text) >= ctx[4]:
        # Same fail-closed rule as a string of the same length, for the same reason: the literal screen is
        # incomplete, and a credential stored as a number is indistinguishable from an id until it is enumerated.
        return ctx[0]
    return value


def _redact(value: Any, ctx: tuple) -> Any:
    if isinstance(value, str):
        out = _screen(value, ctx)
        if ctx[3] and len(out) >= ctx[4]:
            # The literal screen is incomplete (`secret_scan_problems`), so any string long enough to *be* one
            # of the credentials we failed to enumerate is withheld rather than written out unscreened. Shorter
            # strings are safe by construction: `_scan` only ever collects values of `min_value_length`
            # characters or more, so nothing below that length can be one of them.
            return ctx[0]
        return out
    if isinstance(value, dict):
        out: dict = {}
        for key, item in value.items():
            # Keys as well as values: a credential used as a nested mapping key was written verbatim beside its
            # redacted value. A nested key can be data - a measurement keyed by the thing
            # measured - so it gets the same treatment as a value.
            _place(out, _redact_key(key, ctx), _redact(item, ctx))
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(v, ctx) for v in value)
    text = _number_text(value)
    if text is not None:
        # A number was returned unchanged here, which made `pin: 9384756102938475` a way to write a credential
        # into the log that `pin: "9384756102938475"` was refused.
        return _redact_number(value, text, ctx)
    if isinstance(value, bool) or value is None:
        return value
    return _redact(str(value), ctx)


def _redact_key(key: Any, ctx: tuple) -> Any:
    """One mapping key, one level down. Strings and numbers are screened like values - a credential is a
    credential wherever it is written. Any other key type keeps its type rather than being rendered through
    `str()`: `None` and `True` carry nothing to screen, and a key that is neither text nor a number nor one of
    those (a tuple, a date) cannot reach `state/log.jsonl` at all, because `json.dumps` refuses it - so
    stringifying it here would only change what an in-memory record says."""
    if isinstance(key, str) or _number_text(key) is not None:
        return _redact(key, ctx)
    return key


def redact(value: Any, layout: Layout | None = None) -> Any:
    """Replace every credential in a string (or recursively inside a dict/list/tuple, keys included) with the
    placeholder. Numbers are screened as the digits they are and returned unchanged, with their type, unless
    those digits are a credential (`_redact_number`); `bool` and `None` are returned unchanged; anything else is
    rendered with str() first."""
    return _redact(value, _redaction(layout))


def redact_fields(fields: dict, layout: Layout | None = None) -> dict:
    """Redaction for one structured log record (`supervisor.tasks.log`). A record is still written when the
    credential store cannot be screened - losing the audit trail would be the worse failure - but every field
    *value* that could hold a credential is withheld and the reason is recorded in `policy_unscreened`.

    This screens the **keyword fields** of one `tasks.log()` call and everything nested inside them, and that
    is all it screens. A record also carries `actor` and `event`, which are positional parameters and never
    pass through here; `screen_log_identity()` screens those, on the same non-blanking path a field name takes.
    Between them the two functions cover every part of a record except its `ts`.

    The record's own field names are the keyword arguments of `tasks.log`: identifiers written in source code,
    never data. They are screened for literals and patterns like everything else, but the fail-closed blanking
    that applies to values and to nested keys does not apply to them - a record whose keys are all
    `[REDACTED]` says nothing at all, and the one thing this file has to keep doing while the store is
    unreadable is telling the owner what happened."""
    ctx = _redaction(layout)
    out: dict = {}
    for key, value in (fields or {}).items():
        _place(out, _screen_name(key, ctx), _redact(value, ctx))
    if ctx[3]:
        out[UNSCREENED_FIELD] = list(ctx[3])
    return out


def _screen_name(key: Any, ctx: tuple) -> Any:
    """A log record's own top-level field name: screened for literals and patterns, but never blanked by the
    fail-closed rule (see `redact_fields`). A numeric field name is screened as its digits like any other
    value, and replaced whole when they are a credential; anything that is neither text nor a number keeps its
    type."""
    if isinstance(key, str):
        return _screen(key, ctx)
    text = _number_text(key)
    if text is not None and _screen(text, ctx) != text:
        return ctx[0]
    return key


def screen_log_identity(value: Any, layout: Layout | None = None) -> Any:
    """One log record's `actor` or `event` (`supervisor.tasks.log`), screened for literals and patterns.

    These two were the last part of a record written outside the redactor, on the assumption that they are
    identifiers from source code. `actor` is not: `commands.apply_all()` takes it from a queued
    `state/commands/*.json` file, so it is data that reached this machine through the Control Center, and
    `state/log.jsonl` is read by the Control Center, quoted into revision packets and committed in projections -
    a credential that lands there has effectively left `secrets/`.

    Screened, but never blanked: both go through `_screen_name()`, the path a record's own field names take, so
    an unreadable credential store does not turn the audit trail into `[REDACTED] [REDACTED]` at the one moment
    the owner has to read it. The cost is the same one that exemption has always carried - while
    `secret_scan_problems()` is non-empty, an unenumerated credential used as an `actor` is written out. The
    boundary that keeps credential-bearing data out of a queued command is `assert_packet_clean()`, not this."""
    return _screen_name(value, _redaction(layout))


def assert_packet_clean(text: str, layout: Layout | None = None, where: str = "packet", *,
                        job: dict | None = None, task: Any = None, lane: str | None = None) -> str:
    """Return `text` unchanged, or raise PolicyViolation naming *what kind* of credential was found and where.

    `job`, `task` and `lane` are the dispatch context the hosted adapters pass from `prepare()` (the task's `job`
    block, the Task, the adapter name): a project screen may refuse on *what the job is*, not only on the prompt
    text — a job tagged for a channel its content may not leave the machine for, say. Each plugin screen is
    called as `fn(layout, text, where, **ctx)` with exactly the keyword arguments it declares (or all three when it
    takes `**kwargs`); a screen written as `fn(layout, text, where)` keeps working and sees text only.

    A packet is refused, not redacted: a brief that needed a credential to make sense is a design error, and
    silently blanking it would hand the agent a prompt that no longer says what the author meant. Credentials
    reach an agent through its own login, never through packet text (secrets/README.md).

    A credential store that could not be screened refuses *every* dispatch, clean text included: the check
    cannot say the packet is clean if it does not know what the credentials are. Both halves of that judgement
    come from **one** `_scan()` call, so no change to the store can slip in between them.
    """
    values, unscreened = _scan(layout)
    if unscreened:
        raise PolicyViolation(
            f"{where} refused: the credential store could not be screened ({'; '.join(unscreened)}). A "
            "credential that cannot be enumerated cannot be found in a packet, so dispatch fails closed until "
            "secrets/ is readable (docs/runbooks/policy.md §2).")
    hits = _find_in(text, values, layout)
    if hits:
        raise PolicyViolation(
            f"{where} refused: credential-like content ({', '.join(hits)}). Secrets live in secrets/ and are "
            "never referenced from a packet (config/policy.yaml, docs/runbooks/policy.md).")
    ctx = {"job": job, "task": task, "lane": lane}
    for screen in plugin_screens(layout):
        screen(layout, text, where, **_accepted(screen, ctx))     # raises PolicyViolation to refuse
    return text


def _accepted(fn, ctx: dict) -> dict:
    """The subset of `ctx` the screen's signature accepts: named keyword parameters, or everything for `**kwargs`.
    Decided from the signature, never by catching TypeError — a TypeError raised *inside* a screen must surface."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(ctx)
    positional = [n for n, p in params.items() if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                                            inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    return {k: v for k, v in ctx.items() if k in params and k not in positional[:3]}


# ---------------------------------------------------------------------------- plugin screens

def plugin_screens(layout: Layout | None):
    """The project's registered packet screens — or PolicyViolation when a required plugin failed to load: the
    screen that plugin would have registered is not running, so no packet can be called clean."""
    if layout is None:
        return []
    from . import plugins
    try:
        return plugins.assert_loaded(layout).packet_screens
    except plugins.PluginError as exc:
        raise PolicyViolation(f"packet refused: {exc} (fix the plugin or mark it `required: false` in "
                              "project.yaml; `supervisor doctor` shows the error)") from exc


