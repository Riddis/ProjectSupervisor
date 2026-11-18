"""The declared job environment (R-24, ATTEMPT_MODEL.md section 4.4): a process job's subprocess starts from an
allow-listed base plus its lane's own additions, never from the loop's whole environment
(`os.environ`, `adapters/process.py` before this module) — a deny-list is wrong by default, because every new
vendor variable would leak into every lane until someone noticed and added it to a list. An allow-list fails
closed: a project whose tool needs an unusual variable names it once, in `config/routing.yaml`'s `env.base` or
the lane's own `env.lanes.<lane>.add`/`.secrets`.

`SUPERVISOR_*` job-identity markers are added by the framework itself (`adapters.process.job_identity_env`),
never read from this process's own environment and never part of a declared base or lane addition — a job's
identity is minted by the allocator, not inherited.

Nothing here is written to a record or a log: `build()` returns the environment only to the caller that is
about to exec a child with it; the job record keeps only `digest()`'s hash, which identifies a declared
environment without ever holding a value."""
from __future__ import annotations

import hashlib
import os

from .paths import Layout

# Shipped default (config/routing.yaml's own `env.base` overrides it; the template ships this same list so a
# fresh project sees it written out, not only implied). A name absent from this process's own environment is
# simply absent from the job's — never an error.
#
# Beyond the cross-platform shell/locale names, this carries what a plain Windows Python subprocess already
# needs to behave like one rather than a stripped-down one: PATHEXT (so Windows can resolve an extensionless
# command on PATH at all — `COMSPEC`/`PATH` alone are not enough to launch one), SystemDrive/windir (the two
# system-identity names most Windows tooling assumes are set, the same tier as SystemRoot/ComSpec already
# here), NUMBER_OF_PROCESSORS (what a worker reads to size its own parallelism, absent any job-specific
# override), and PYTHONPATH/PYTHONIOENCODING/APPDATA/LOCALAPPDATA — already load-bearing for a *different*
# framework-launched child elsewhere (`plugins.import_side_effects`'s probe sets `PYTHONPATH`;
# `plugin_template/tests/test_conformance.py-tpl` and every in-repo plugin's own conformance test set
# `PYTHONIOENCODING`; a Python install's own user site-packages/cache resolution needs `APPDATA`/
# `LOCALAPPDATA` on Windows), so narrowing a process job to less than that would be a worse regression than
# this release's own fix. An upgrading project that already relies on something outside even this widened set
# still names it once, in its own `env.base` (see the release doc's upgrade note, 2.8.0 step 12).
DEFAULT_BASE: tuple[str, ...] = ("PATH", "SystemRoot", "ComSpec", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG",
                                "LC_ALL", "TZ", "PYTHONHASHSEED", "PYTHONPATH", "PYTHONIOENCODING", "APPDATA",
                                "LOCALAPPDATA", "PATHEXT", "SystemDrive", "windir", "NUMBER_OF_PROCESSORS")

# A job's own identity, minted by the allocator and added by adapters.process.job_identity_env — never inherited
# from the loop's launching environment, which is exactly what makes a launching process that already carries
# one of these a foreign supervisor's marker rather than this loop's own (SUPERVISOR_ROOT is excluded: it is
# the loop's own self-reference, set by its own launcher or the owner to point this process at a project, never
# evidence of being nested inside another supervisor's job; see verify.JOB_CONTROL_ENV_VARS, which drops it for
# a declared test's environment for the same reason, under the launching process's own target rather than
# another's).
FOREIGN_MARKERS: tuple[str, ...] = ("SUPERVISOR_ATTEMPT", "SUPERVISOR_JOB", "SUPERVISOR_ATTEMPT_NO",
                                    "SUPERVISOR_RUN_DIR", "SUPERVISOR_PROGRESS_FILE")


def _routing(layout: Layout | None) -> dict:
    from . import gate
    return gate.routing_doc(layout)


def base_names(layout: Layout | None = None) -> tuple[str, ...]:
    """The declared base allow-list: `config/routing.yaml` `env.base`, or `DEFAULT_BASE` when the project's
    routing.yaml has no `env` section (a project that predates this release)."""
    names = (_routing(layout).get("env") or {}).get("base")
    if not names:
        return DEFAULT_BASE
    return tuple(str(n) for n in names if isinstance(n, str) and n.strip())


def _named_secrets(layout: Layout, names: list[str]) -> dict[str, str]:
    """`KEY=VALUE` pairs named in `env.lanes.<lane>.secrets`, read from `secrets/.env` — never logged, never
    stored: the caller folds the values straight into the subprocess environment, and `digest()` only ever
    sees the hash it produces, not the values themselves."""
    path = layout.secrets / ".env"
    if not names or not path.exists():
        return {}
    pairs: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            pairs[key] = value
    return {name: pairs[name] for name in names if name in pairs}


def lane_additions(layout: Layout, lane: str) -> dict[str, str]:
    """`config/routing.yaml` `env.lanes.<lane>.add` (literal values) plus `.secrets` (named values read from
    `secrets/.env`) — the per-lane growth of the declared environment. `{}` for a lane with no entry."""
    lanes = (_routing(layout).get("env") or {}).get("lanes") or {}
    entry = lanes.get(lane) if isinstance(lanes, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    add = {str(k): str(v) for k, v in (entry.get("add") or {}).items()}
    secret_names = [str(n) for n in (entry.get("secrets") or []) if isinstance(n, str) and n.strip()]
    add.update(_named_secrets(layout, secret_names))
    return add


def digest(env: dict[str, str]) -> str:
    """Sorted names plus a hash of values: identifies a declared environment without writing any value anywhere
    (ATTEMPT_MODEL.md section 4.4). Two builds of the same declaration hash the same; adding, removing or
    changing one name or one value always moves it."""
    h = hashlib.sha256()
    for key in sorted(env):
        h.update(key.encode("utf-8"))
        h.update(b"\0")
        h.update(env[key].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def build(layout: Layout, lane: str) -> tuple[dict[str, str], str]:
    """The declared environment a `lane` job's subprocess runs with (the allow-listed base, read from this
    process's own environment, plus the lane's own additions) and its digest. Carries no `SUPERVISOR_*`: those
    are the caller's to add (`adapters.process.job_identity_env`), after this returns."""
    env = {name: os.environ[name] for name in base_names(layout) if name in os.environ}
    env.update(lane_additions(layout, lane))
    return env, digest(env)


def foreign_markers(env: dict[str, str] | None = None) -> list[str]:
    """Names of `FOREIGN_MARKERS` already set in `env` (default: this process's own environment), sorted — a
    loop about to start checks its own launching environment with this before it ever dispatches a job
    (ATTEMPT_MODEL.md section 4.4): a desktop session, or a shell, that still carries another job's identity
    markers (H-001) would otherwise hand them down to every job this loop launches."""
    source = os.environ if env is None else env
    return sorted(k for k in source if k in FOREIGN_MARKERS)
