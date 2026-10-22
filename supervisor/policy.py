"""The policy boundary: credential isolation. `config/policy.yaml` is the data; this module is the only mechanism.

Three guards, each at the one boundary the thing it protects has to cross:

* **packets** - `supervisor.packets` passes every brief through `assert_packet_clean()`, so a dispatch whose prompt
  carries a credential (a literal value from `secrets/`, a secret-shaped string, or a secret-suffixed
  environment value) is refused before any process is started. Credentials reach an agent child through its
  subscription login only, never through text (`adapters.process.scrub_env` strips the environment).
* **logs** - `supervisor.tasks.log()` redacts every field through `redact_fields()` on the single append path to
  `state/log.jsonl`.
* **extension screens** - a project may register further packet screens (`registry.packet_screen`) for content
  that must not leave the machine; `assert_packet_clean()` runs them after the credential check.

Everything fails closed. The defaults below apply when `config/policy.yaml` is missing or unreadable.

Where a configured value is a list of *things to refuse or strip* - secret patterns, secret key words, secret
environment suffixes - it is **unioned** with the built-in floor: a config edit there can only tighten, and removing
a built-in needs a code change in `DEFAULTS`. The three scalars in the secrets section whose natural direction is
*loosening* are **clamped** rather than trusted - `min_value_length` can only be lowered, `ignore_files` may never
hide a dotenv file, and an empty `redaction_placeholder` falls back to the built-in (`_clamped_secrets()`).

A configured entry may only replace a floor entry **of the same kind** (`_kind`); a mismatch is dropped, the floor
stands, and the mismatch is reported by `config_problems()` (shown by `supervisor doctor`).
"""
from __future__ import annotations

import json
import os
import re
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


def _stamp(path: Path) -> tuple:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return ()


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
    protect and are taken as given (a project extension may read them through `section()`)."""
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
    stamp = _stamp(path)
    cached = _doc_cache.get(path)
    if cached and cached[0] == stamp:
        return cached[1], cached[2]
    configured: dict = {}
    problems: list[str] = []
    if stamp:
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
    _doc_cache[path] = (stamp, doc, problems)
    return doc, problems


def load(layout: Layout | None = None) -> dict:
    """The effective policy: built-in floor merged with `config/policy.yaml`. An unreadable, malformed or
    wrong-shaped config leaves the floor in place rather than opening anything up."""
    return _loaded(layout)[0]


def config_problems(layout: Layout | None = None) -> list[str]:
    """Human-readable descriptions of every `config/policy.yaml` entry that was rejected (unparseable file,
    wrong kind for the floor entry it would replace). Each one means a value the owner wrote is *not* in
    effect."""
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


def _pairs_from_json(data: Any, minimum: int, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(data, str):
        return [(prefix or "#0", data)] if len(data) >= minimum else []
    if isinstance(data, dict):
        return [p for k, v in data.items() for p in _pairs_from_json(v, minimum, f"{prefix}.{k}" if prefix else str(k))]
    if isinstance(data, list):
        return [p for i, v in enumerate(data) for p in _pairs_from_json(v, minimum, f"{prefix}[{i}]")]
    return []


_values_cache: dict[Path, tuple[tuple, dict[str, str]]] = {}


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
    scanned if it is named like a credential (`docs/runbooks/policy.md` §2)."""
    if any(pattern.search(value) for _name, pattern in secret_patterns(layout)):
        return True
    if _looks_secret(file_name, layout):
        return True
    return not label.startswith("#") and _looks_secret(label, layout)


def secret_values(layout: Layout | None = None) -> dict[str, str]:
    """Literal credential values currently in `secrets/`, keyed by `<file>:<key>` label (the label is logged,
    the value never is). Cached on the directory's file stamps, so a key written a moment ago is seen at once.

    This is the strongest of the three checks: a value named like a credential is caught whether or not it
    looks like one (`_is_literal_secret` for what "named like" means)."""
    directory = secrets_dir(layout)
    cfg = _clamped_secrets(layout)
    ignore = {str(n) for n in (cfg.get("ignore_files") or [])}
    minimum = int(cfg["min_value_length"])
    try:
        files = sorted(p for p in directory.iterdir() if p.is_file() and p.name not in ignore)
    except OSError:
        files = []
    # The config takes part in the stamp: which pairs count depends on the key words and patterns, so an edit to
    # config/policy.yaml must invalidate this cache as surely as a new file in secrets/ does.
    stamp = (_stamp(config_path(layout)), tuple((p.name, *_stamp(p)) for p in files))
    cached = _values_cache.get(directory)
    if cached and cached[0] == stamp:
        return cached[1]
    found: dict[str, str] = {}
    for path in files:
        try:
            if path.stat().st_size > 1_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pairs: list[tuple[str, str]] = []
        if path.suffix.lower() == ".json":
            try:
                pairs = _pairs_from_json(json.loads(text), minimum)
            except ValueError:
                pairs = []
        if not pairs:
            pairs = _pairs_from_text(text, minimum)
        for key, value in pairs:
            if _is_literal_secret(path.name, key, value, layout):
                found[f"{path.name}:{key}"] = value
    _values_cache[directory] = (stamp, found)
    return found


def find_secrets(text: str, layout: Layout | None = None) -> list[str]:
    """Labels of every credential found in `text` - pattern names and `secrets/` / environment labels. Never
    returns the matched value: the whole point is that it stays out of messages, exceptions and logs."""
    if not text:
        return []
    hits: list[str] = []
    for label, value in secret_values(layout).items():
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


def redact(value: Any, layout: Layout | None = None) -> Any:
    """Replace every credential in a string (or recursively inside a dict/list/tuple) with the placeholder.
    Non-string scalars are returned unchanged; anything else is rendered with str() first."""
    mark = placeholder(layout)
    if isinstance(value, str):
        out = value
        for literal in list(secret_values(layout).values()) + list(env_secret_values(layout).values()):
            if literal and literal in out:
                out = out.replace(literal, mark)
        for _name, pattern in secret_patterns(layout):
            out = pattern.sub(mark, out)
        return out
    if isinstance(value, dict):
        return {k: redact(v, layout) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v, layout) for v in value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value), layout)


def redact_fields(fields: dict, layout: Layout | None = None) -> dict:
    """Redaction for one structured log record (`supervisor.tasks.log`)."""
    return {k: redact(v, layout) for k, v in (fields or {}).items()}


def assert_packet_clean(text: str, layout: Layout | None = None, where: str = "packet") -> str:
    """Return `text` unchanged, or raise PolicyViolation naming *what kind* of credential was found and where.

    A packet is refused, not redacted: a brief that needed a credential to make sense is a design error, and
    silently blanking it would hand the agent a prompt that no longer says what the author meant. Credentials
    reach an agent through its own login, never through packet text (secrets/README.md)."""
    hits = find_secrets(text, layout)
    if hits:
        raise PolicyViolation(
            f"{where} refused: credential-like content ({', '.join(hits)}). Secrets live in secrets/ and are "
            "never referenced from a packet (config/policy.yaml, docs/runbooks/policy.md).")
    for screen in extension_screens(layout):
        screen(layout, text, where)                     # raises PolicyViolation to refuse
    return text


# ---------------------------------------------------------------------------- extension screens

def extension_screens(layout: Layout | None):
    """The project's registered packet screens — or PolicyViolation when a required extension failed to load: the
    screen that extension would have registered is not running, so no packet can be called clean."""
    if layout is None:
        return []
    from . import extensions
    try:
        return extensions.assert_loaded(layout).packet_screens
    except extensions.ExtensionError as exc:
        raise PolicyViolation(f"packet refused: {exc} (fix the extension or mark it `required: false` in "
                              "project.yaml; `supervisor doctor` shows the error)") from exc


