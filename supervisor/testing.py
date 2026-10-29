"""Test helpers for plugins: UNSTABLE.

Packaged and project plugins write their behaviour tests against these helpers. Unlike `supervisor.plugin`, this
module carries no compatibility promise: it may change in any release, minor ones included, and gate G2 does not
record it. It is promoted from the framework's own test fixtures, which use it too.

    from supervisor import testing

    def test_my_lane_completes(tmp_path):
        lay = testing.project(tmp_path, modules={"my_ext": SOURCE})
        with testing.lane(testing.FakeLane("render", artifacts=["out.png"])):
            testing.make_task(lay, "A", job={"adapter": "render"}, artifacts=["out.png"])
            testing.drive(lay, until=lambda: ...)

Nothing here launches a model client or reaches the network. The Control Center test client is imported only when
`ui_client()` is called (it needs the framework's `ui` and `dev` extras)."""
from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from pathlib import Path

import yaml

from . import plugins as _plugins
from . import project as _project
from . import store
from ._vendor.contract import CapacityDeferred
from .adapters.base import REGISTRY, Adapter, Result
from .paths import Layout

CSRF = "test-csrf-value"                  # the token ui_client() sets as the CSRF cookie; post it as `csrf_token`

_GATES = {"phases": {0: {"requires": []}, 1: {"requires": ["G0"]}, 9: {"requires": ["G6"]}},
          "gates": {"G0": {"status": "OPEN", "phase": 0}, "G6": {"status": "OPEN", "phase": 6}}}


def project(root: str | Path, *, name: str | None = None, plugins: list | None = None,
            modules: dict[str, str] | None = None, files: dict[str, str] | None = None,
            routing: dict | None = None, policy: dict | None = None) -> Layout:
    """A temporary project at `root`: the state folders, an open phase 0 (state/gates.yaml), no owner tasks.
    `modules` writes `plugins/<name>.py` for each source and lists them in project.yaml (or list `plugins`
    yourself, e.g. with `required: false`); `files` writes root-relative text files; `routing` and `policy` write
    config/routing.yaml and config/policy.yaml. project.yaml is written only when `name`, `plugins` or `modules`
    is given. Loaded plugins are forgotten (`reset()`), so the next load reads this project."""
    lay = Layout(Path(root)).ensure()
    (lay.state / "gates.yaml").write_text(yaml.safe_dump(_GATES), encoding="utf-8")
    (lay.state / "owner_tasks.yaml").write_text("owner_tasks: []\n", encoding="utf-8")
    for rel, text in (files or {}).items():
        path = lay.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for key, doc in (("routing.yaml", routing), ("policy.yaml", policy)):
        if doc is not None:
            lay.config.mkdir(parents=True, exist_ok=True)
            (lay.config / key).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    if name is not None or plugins is not None or modules:
        _write_modules(lay, modules or {})
        doc = {"name": name or "proj"}
        doc["plugins"] = list(plugins) if plugins is not None else list(modules or {})
        _project.write(lay, doc)
    _plugins.reset()
    return lay


def _write_modules(lay: Layout, modules: dict[str, str]) -> None:
    for name, source in modules.items():
        path = lay.plugins / (name.replace(".", "/") + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def add_plugins(layout: Layout, modules: dict[str, str], entries: list | None = None,
                   files: dict[str, str] | None = None) -> Layout:
    """Write `plugins/<name>.py` for each module (and root-relative `files`), list `entries` (default: the
    modules) in project.yaml, keeping its other keys, and forget loaded plugins."""
    _write_modules(layout, modules)
    for rel, text in (files or {}).items():
        path = layout.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    doc = dict(_project.load(layout)) if _project.exists(layout) else {}
    doc["name"] = doc.get("name") or "proj"
    doc["plugins"] = list(entries) if entries is not None else list(modules)
    _project.write(layout, doc)
    _plugins.reset()
    return layout.ensure()


def make_task(layout: Layout, tid: str, phase: int = 0, deps=(), reviewer=(), human: str = "none",
              status: str = "NOT_STARTED", **fields) -> dict:
    """A task in state/tasks/: the minimal valid contract (owner_role engineer, artifacts ["x"], phase 0, cost
    category deterministic), with any other task field given as a keyword (`job={...}`, `artifacts=[...]`,
    `owned_paths=[...]`, `cost_category=...`)."""
    doc = {"id": tid, "phase": phase, "title": tid, "objective": "o", "owner_role": "engineer",
           "depends_on": list(deps), "actions": ["a"], "artifacts": ["x"], "tests": ["t"], "reviewer": list(reviewer),
           "acceptance": "ok", "escalation": "none", "human_approval": human, "cost_category": "deterministic",
           "status": status}
    doc.update(fields)
    return store.write_new(layout.tasks / f"{tid}.yaml", doc)


class FakeLane(Adapter):
    """An in-process lane that does what it is told: write `artifacts` (root-relative paths) and finish with
    `status`, `notes` and `error`; or raise CapacityDeferred from submit() the first `defer` times (re-tried from
    `defer_until`, epoch seconds). `submitted` lists the attempts it ran, `deferred` counts deferrals."""

    def __init__(self, name: str = "fake_lane", *, status: str = "COMPLETE", artifacts=(), notes: str = "",
                 error: str | None = None, defer: int = 0, defer_until: float = 0.0, gpu: bool = False):
        self.name, self.status, self.artifacts, self.notes, self.error = name, status, list(artifacts), notes, error
        self.defer, self.defer_until, self.gpu = defer, defer_until, gpu
        self.submitted: list[str] = []
        self.deferred = 0

    def prepare(self, layout, task, attempt_id):
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
                "root": str(layout.root)}

    def submit(self, spec):
        if self.deferred < self.defer:
            self.deferred += 1
            raise CapacityDeferred(self.defer_until, f"{self.name}: no capacity (fake)")
        for rel in self.artifacts:
            path = Path(spec["root"]) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{spec['attempt_id']}\n", encoding="utf-8")
        self.submitted.append(spec["attempt_id"])
        return {"kind": "fake", "attempt_id": spec["attempt_id"]}

    def alive(self, handle):
        return False

    def collect(self, spec, handle):
        return Result(status=self.status, artifacts=list(self.artifacts), notes=self.notes, error=self.error)


@contextlib.contextmanager
def lane(adapter: Adapter) -> Iterator[Adapter]:
    """Register `adapter` as a lane for the block (replacing any lane of that name), then restore what was there."""
    before = REGISTRY.get(adapter.name)
    REGISTRY[adapter.name] = adapter
    try:
        yield adapter
    finally:
        if before is None:
            REGISTRY.pop(adapter.name, None)
        else:
            REGISTRY[adapter.name] = before


def drive(layout: Layout, iterations: int = 10, until: Callable[[], bool] | None = None):
    """Run the loop for at most `iterations` iterations without sleeping, stopping early once `until()` is true.
    Returns the Supervisor."""
    from .loop import Supervisor
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(iterations):
        sup.once()
        if until is not None and until():
            break
    return sup


def ui_client(layout: Layout, csrf: str = CSRF):
    """A Control Center test client for this project (FastAPI's TestClient on 127.0.0.1), with `csrf` as the CSRF
    cookie: post it as the `csrf_token` form field."""
    from fastapi.testclient import TestClient

    from .ui.app import create_app
    from .ui.auth import CSRF_COOKIE
    client = TestClient(create_app(layout), base_url="http://127.0.0.1")
    client.cookies.set(CSRF_COOKIE, csrf)
    return client


def load(layout: Layout):
    """Set the project's plugins up as the loop does (their lanes, checks, kinds and commands registered).
    Returns the loader's registry: `.loaded` and `.errors` say what loaded."""
    return _plugins.load(layout)


def registered_lane(name: str) -> Adapter:
    """The lane registered under `name` (after `load()`), the object the loop calls. KeyError when there is none."""
    if name not in REGISTRY:
        raise KeyError(f"no lane {name!r} is registered (loaded: {sorted(REGISTRY)})")
    return REGISTRY[name]


def task(layout: Layout, tid: str):
    """The task as stored, the object a lane's prepare() is handed: `.status`, `.job`, `.artifacts` and the rest."""
    from . import tasks
    return tasks.load(layout, tid)


def job(layout: Layout, attempt_id: str) -> dict:
    """One job record (state/jobs/<attempt>.json): `phase` (PREPARED, SUBMITTED, RUNNING, DONE, FAILED), `result`
    once done, `spec`, `handle`, `error`."""
    from . import jobs as _jobs
    return _jobs.read(layout, attempt_id)


def jobs(layout: Layout, task_id: str | None = None) -> list[dict]:
    """The task's job records, oldest attempt first (every record without `task_id`)."""
    from . import jobs as _jobs
    return _jobs.for_task(layout, task_id) if task_id is not None else _jobs.all_records(layout)


def running_job(layout: Layout, task_id: str, adapter: str, spec: dict, handle: dict, *,
                attempt_id: str | None = None, gpu: bool = False, **fields) -> dict:
    """Plant an open job record the way the loop leaves one after a lane's submit(): phase RUNNING with `spec` and
    `handle` (and any other record field), the task IN_PROGRESS. The attempt id defaults to the task's next one
    (`<task>.a<n>`). `gpu=True` makes it hold the resource slot. Returns the record."""
    from . import jobs as _jobs
    from . import tasks
    attempt_id = attempt_id or f"{task_id}.a{len(_jobs.for_task(layout, task_id)) + 1}"
    _jobs.prepare(layout, task_id, attempt_id, adapter, spec, gpu=gpu)
    rec = _jobs.set_phase(layout, attempt_id, "RUNNING", handle=handle, **fields)
    if tasks.load(layout, task_id).status in ("NOT_STARTED", "READY"):
        tasks.set_status(layout, task_id, "IN_PROGRESS", actor="loop", note=f"{attempt_id} planted by a test")
    return rec


def settle(layout: Layout) -> None:
    """One settle pass of the loop, without dispatching: each open job record is collected or adopted through its
    lane (a finished one closed, an expired one cancelled), as a loop restart or iteration does first."""
    from .loop import Supervisor
    Supervisor(layout, sleep_s=0).settle()


def plan(layout: Layout) -> list[tuple[str, str]]:
    """What the loop would dispatch now, as (task id, lane) pairs: the scheduler's picks after the gate's checks
    (dependencies, phase gates, limits and the resource slot against the open job records)."""
    from . import scheduler
    return [(t.id, lane_name) for t, lane_name in scheduler.plan(layout)]


def inbox_item(layout: Layout, item_id: str) -> dict:
    """One inbox item as stored (any kind). Raises supervisor.plugin.InboxError when there is none."""
    from . import inbox
    return inbox.load(layout, item_id)


def make_inbox_item(layout: Layout, *, kind: str = "question", subject: str = "subject", summary: str = "summary",
                    allowed_responses: list[str] | None = None, for_role: str = "owner", **fields) -> dict:
    """An open inbox item of `kind` for `for_role`, with any other field (`items=[...]`, `rows=[...]`) written on it
    under the inbox lock, as a plugin's code or an agent's request would leave it. Returns it as stored."""
    from . import inbox
    item = inbox.create(layout, for_role=for_role, subject=subject, summary=summary, kind=kind,
                        allowed_responses=allowed_responses)
    if fields:
        store.update(inbox.path_for(layout, item["id"]), lambda doc: {**doc, **fields},
                     lock_path=layout.state / ".inbox.lock")
    return inbox.load(layout, item["id"])


def answer(layout: Layout, item_id: str, response: str, note: str = "", by: str = "owner") -> dict:
    """The owner's generic answer to an inbox item, the path the inbox page and `inbox.answer` take (not an owning
    plugin's close_owned_item): a kind claimed with `answerable=False` refuses it with InboxError."""
    from . import inbox
    return inbox.answer(layout, item_id, response, note=note, by=by)


@contextlib.contextmanager
def loop_lock(layout: Layout) -> Iterator[None]:
    """Hold the project's state lock for the block, as a running loop does: an owner command submitted meanwhile
    (supervisor.plugin.submit_command, a Control Center action) is queued for the loop, not applied."""
    from .locks import FileLock
    with FileLock(layout.lock, owner="test loop"):
        yield


def import_side_effects(layout: Layout, name: str) -> dict[str, list[str]]:
    """What importing plugin `name` registers by itself, measured in a fresh interpreter (all lists empty for an
    plugin that registers only in setup()). See `supervisor.plugins.import_side_effects`."""
    return _plugins.import_side_effects(layout, name)


def reset() -> None:
    """Forget loaded plugins and the lanes, python jobs and commands they registered, like a fresh process."""
    _plugins.reset()
    _plugins.forget_registrations()
