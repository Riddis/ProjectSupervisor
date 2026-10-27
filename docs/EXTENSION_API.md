# The public extension API (`supervisor.ext`)

An extension imports `supervisor.ext` and nothing else from `supervisor` (besides `supervisor.testing` in its tests,
which is explicitly unstable). Everything listed here is a promise: gate G2 (`python -m supervisor.api_contract
check`, `docs/BOUNDARY.md`) keeps `supervisor.ext.__all__` equal to the recorded contract in `docs/extension_api.json`
and compares both it and this document with the last release's. A name is removed, or its signature changed in a way
that breaks a caller, only in a major release, after a minor release that warns; an addition comes in a minor release
with an "Extension API" entry in `CHANGELOG.md`. Each name has its own `###` section here; a change to what a section
promises (types, data shapes, semantics, error cases) is named in an "Extension API" entry too, marked `(editorial)`
for wording only, `(breaking)` for a major release, or unmarked for a compatible change. Anything not listed here is
internal and may change in any release.

```python
from supervisor.ext import Adapter, Check, Layout, Result, atomic_json, gpu
```

**Internal imports are deprecated (1.11.0).** An extension that still imports anything else from `supervisor`
(`supervisor.inbox`, `supervisor.ui.deps`, `supervisor._vendor.contract`, ...) loads and works, but the loader issues
a `DeprecationWarning` at that file and line, and `supervisor doctor` shows a WARN (`extension_imports`) naming each
import. The scan reads the source of every module in the project's `extensions/` folder, helpers included, and of
installed extension modules. A later major release turns the WARN into a FAIL.

The API is introduced in 1.11.0. Where a name is the framework's own object, re-exported unchanged, it says so: the
behaviour described is the framework's, and a test reaches it through `supervisor.ext` (`tests/test_ext_reexports.py`).

## Registration

### `Registry`

The object `setup(registry)` receives: an extension registers everything through it, from `setup()` only. Importing
an extension module must change nothing, no registered lane, job or command and no started process, because the
loader may import a module to inspect it before deciding to set it up. `supervisor.extensions.import_side_effects()`
imports a module in a fresh interpreter and names every lane, python job and command kind the import itself
registered; the reference extension's test holds it to none. The `Registry` exported here is a `typing.Protocol`, for
type hints and documentation; the loader's own registry has exactly these signatures (a test keeps them equal).
Extensions never construct one.

| Hook | Registers |
|---|---|
| `layout` | the project's `Layout` (an attribute, not a hook) |
| `config_sections(*names)` | the `config/routing.yaml` sections this extension reads with `config()` |
| `adapter(adapter)` | a job lane under `adapter.name` (see `Adapter`) |
| `python_job(name, fn)` | a deterministic job `fn(root, run_dir, **params) -> {"status", "artifacts", "notes"}` for `job: {adapter: python, fn: <name>, params: {...}}` |
| `doctor_check(fn)` | `fn(layout) -> Check` |
| `overview_panel(fn)` | `fn(layout) -> {"title", "rows": [(label, value)], "progress": (value, max)?}` on the Overview |
| `packet_screen(fn)` | `fn(layout, text, where, *, job=None, task=None, lane=None)`, raising to refuse a brief |
| `projection(fn, path=None, name=None)` | `fn(layout) -> Path`, an extra rendered markdown view |
| `cli(app, name)` | a Typer app as `supervisor <name> ...` |
| `command(kind, fn)` | an owner command kind `fn(layout, payload, actor) -> dict`, idempotent by `payload["_id"]` |
| `inbox_kind(kind, label, page=None, answerable=False, show_evidence=True, score_keys=())` | an inbox item kind the extension creates and owns |
| `ui_router(router, label, path)` | a FastAPI router and its sidebar entry |
| `template_dir(path)` | Jinja templates, loaded after the framework's |
| `static_dir(path, prefix)` | static files at `/static/<prefix>/...` |

`docs/FRAMEWORK_VS_EXTENSION.md` describes each hook in full. Names are unique: `adapter()` refuses a lane name
another lane already has, built in or another extension's, and `python_job()`, `command()`, `inbox_kind()` and
`config_sections()` refuse a taken name the same way. A refusal is an `ExtensionError` from `setup()`: the extension
does not load, and a required one blocks the loop. Registering the same object a second time is harmless, so an
extension that still registers at import as well keeps working. Module-level `register(adapter)` and `@job(name)`
from the internal modules still work and are deprecated from 1.11.0.

### `ExtensionError`

A `RuntimeError`: what the registry raises for a refused registration, and what `config()` raises for a refused
read. Raised from `setup()`, it stops that extension loading.

## Configuration

Until phase 3 gives each extension its own configuration (`extensions.<identity>` with a schema), an extension
reads the sections of `config/routing.yaml` it declared, and the shared resource policy. The signatures below stay
when phase 3 changes where the values come from.

```python
from supervisor import ext

def setup(registry):
    registry.config_sections("render", "ffmpeg")      # this extension's sections of config/routing.yaml

def render_config(layout):
    return ext.config(layout, "render")               # {} when routing.yaml has no `render:` section
```

### `config`

`config(layout, section) -> dict`: the `section` of `config/routing.yaml`, parsed for this call: a copy, so
changing it changes nothing, and a later call sees the file as it is then. `{}` when the file or the section is
absent. Raises `ExtensionError` when:

- the caller is not an extension's code. The caller is the configured extension whose module is running: a module
  listed under `extensions:` in `project.yaml` (recognised by its source file, so the same file imported under another
  name, as a project's tests may, is the same extension), or a helper module it calls, which reads for that
  extension;
- the calling extension did not declare `section` with `registry.config_sections()`, including a section another
  extension declared and one the framework owns;
- the section is not a mapping, or `routing.yaml` is not valid YAML.

`registry.config_sections(*names)` takes routing.yaml keys (letters, digits, `_`, `-`). It refuses a framework
section (every top-level key of the template's `routing.yaml`, such as `roles`, `limits`, `capacity`, `timeouts`,
plus `gpu`, `resources` and `extensions`) and a section another extension already declared: every setting has one
owner.

### `resources`

`resources(layout) -> dict`: the shared resource policy, open to every extension: `{"gpu": {...}}`, a copy of
`routing.yaml`'s `gpu` section (`{}` when absent), parsed for this call. Phase 3 moves it to `resources.gpu`; this
shape stays.

## Project paths

### `Layout`

`Layout(base: Path | None = None)`: the paths of one project. `base` is the project root (resolved to an absolute
path; a relative one against the current directory); without it, the root is found from `SUPERVISOR_ROOT` or the
nearest folder above the current directory holding `project.yaml`, and `NoProject` is raised when there is none.
Every attribute is a `pathlib.Path` under `root`; nothing is created until `ensure()`.

| Attribute | Path | Attribute | Path |
|---|---|---|---|
| `root` | the project root | `project_file` | `project.yaml` |
| `config` | `config/` | `agents` | `agents/` |
| `runs` | `runs/` (per-attempt run folders, git-ignored) | `docs` | `docs/` |
| `secrets` | `secrets/` | `uploads` | `uploads/` |
| `extensions` | `extensions/` | `state` | `state/` |
| `tasks` | `state/tasks/` | `decisions` | `state/decisions/` |
| `issues` | `state/issues/` | `inbox` | `state/inbox/` |
| `commands` | `state/commands/` | `commands_applied` | `state/commands/applied/` |
| `conversations` | `state/conversations/` | `gates` | `state/gates.yaml` |
| `owner_tasks` | `state/owner_tasks.yaml` | `log` | `state/log.jsonl` |
| `lock` | `state/.lock` | `render_lock` | `state/.render.lock` |
| `runtime` | `state/runtime/` | `jobs` | `state/runtime/jobs/` |
| `wake` | `state/runtime/wake` | | |

`ensure() -> Layout` creates the state folders and `runs/` and returns the layout. An extension reads under `state/`
through the operations of this API, never by writing those files itself: the loop is their single writer.

## Files

The framework's own file primitives (`supervisor/_vendor/contract.py`), re-exported unchanged.

### `atomic_write_text`

`atomic_write_text(path, text, newline=None, keep=False) -> None`: writes `text` (UTF-8) to a fresh temporary file
beside `path` and replaces `path` with it in one step, so a reader never sees a half-written file and an interrupted
write leaves the old file whole. On Windows a replace is refused while another process holds the file open; the
write retries for about four seconds before raising. Missing parent folders are created. It prevents torn files, not
lost updates: two writers of one file must coordinate themselves.

- `newline`: as `open()`'s. `None` writes `"\n"` as the platform's line ending; `""` writes `text` exactly as given;
  `"\r\n"` or `"\n"` force one.
- `keep=True`: the new file keeps the old one's owner, group and permissions (POSIX mode bits and, on Linux, ACLs
  and extended attributes; the Windows DACL), a symlink's target is replaced and the link stays, and a file with
  several hard links is refused. When any of that cannot be done it raises a `PermissionError` subclass and leaves
  the old file as it was.
- Errors: the `OSError` of the failed write or replace; the temporary file is removed first.

### `atomic_json`

`atomic_json(path, data) -> None`: `data` as indented JSON (`json.dumps(data, indent=2)`), written with
`atomic_write_text`.

### `read_json`

`read_json(path)`: the parsed JSON of a UTF-8 file; a leading byte-order mark is accepted. Raises `OSError` when the
file cannot be read and `ValueError` (`json.JSONDecodeError`) when it is not JSON.

### `contained`

`contained(path, roots, root=None) -> bool`: whether `path` is one of `roots` or inside one. Relative paths (in
`path` and `roots`) resolve against `root`, or the current directory without it; `..` is resolved first, so a path
that climbs out of a root is not contained, and a sibling sharing a name prefix (`proj2` beside `proj`) is not inside
it. Raises `ValueError` when a path traverses a symbolic link or junction: containment is never decided through one.

## Job lanes

An extension adds a lane with `registry.adapter(...)` from its `setup(registry)` (`docs/FRAMEWORK_VS_EXTENSION.md`).
The loop calls the methods below; it never calls them concurrently for one job.

### `Adapter`

The base class of every lane, for in-process or HTTP work (subclass `ProcessAdapter` for a subprocess).

| Member | Meaning |
|---|---|
| `name` | the lane's name: the adapter tasks and routing refer to (`job: {adapter: <name>}`) |
| `gpu` | `True` holds the project's single heavy-resource slot while a job runs (`limits.max_resource_jobs`) |
| `prepare(layout, task, attempt_id) -> dict` | the job's spec: a JSON-serialisable dict saved in the job record before launch. It must contain `adapter`, `task_id`, `attempt_id` and `run_dir` (usually `layout.runs / task.id / attempt_id`, created here) |
| `submit(spec) -> dict` | start the job; return a JSON-serialisable handle. Raise `CapacityDeferred` when it cannot start for now |
| `alive(handle) -> bool` | whether the job is still running |
| `collect(spec, handle) -> Result \| None` | `None` while running, else the `Result` |
| `recover(spec) -> dict \| None` | after a crash between `submit()` and the record of its handle: the handle of work already launched for this spec, or `None` when nothing was. Default `None` |
| `terminate(handle) -> None` | stop the job (owner cancel, timeout). Default: nothing |
| `lane_ready(layout) -> bool` | for a lane that deferred with lane-wide unavailability: whether it could serve now, before the saved time. Cheap and side-effect free. Default `False` |
| `reset_lane(layout) -> bool` | the owner command `lane.reset`: forget a lane-wide unavailability; whether there was one. Default `False` |

An exception from `prepare()`, `submit()`, `collect()` or `recover()` fails that one attempt (the job record is
`FAILED` with the exception text), not the loop.

### `ProcessAdapter`

An `Adapter` that runs a subprocess with crash-safe handles: the child's pid and creation time are kept in
`run_dir/status.json`, so a restarted loop re-adopts a running child (`recover`) instead of launching a second one.
Output goes to `run_dir/stdout.jsonl` and `run_dir/stderr.txt`; the job ends when the child writes
`run_dir/result.json` (`{"status", "artifacts", "notes", "error"}`) and exits, when it is idle for the spec's
`idle_s`, or at its hard cap (`timeout_s`). Override:

| Member | Meaning |
|---|---|
| `command(spec) -> list[str]` | the argv to run. Default `spec["argv"]` |
| `env(spec) -> dict` | the child's environment. Default: the loop's, plus the attempt id the recovery uses; keep those keys when overriding |
| `parse_result(spec, handle) -> Result` | the `Result` after the child exits. Default: read `result.json`, or `FAILED` without one |
| `on_timeout(spec, handle) -> None` | salvage partial output before a timeout's `FAILED` result. Default: nothing |
| `limits(spec, handle) -> tuple` | `(hard-cap deadline epoch, idle seconds or None, idle CPU-seconds threshold)` |
| `handoff_before_cap` | `True` asks the job for a handoff shortly before its hard cap. Default `False` |

`prepare`, `submit`, `alive`, `collect`, `recover` and `terminate` are implemented; a subclass usually overrides
`prepare` (to build its spec, which `submit` reads: `run_dir`, `root` as the working directory, `timeout_s`,
optional `idle_s` and `stdin_file`) and `command`.

### `Result`

A dataclass, what `collect()` returns.

| Field | Meaning |
|---|---|
| `status: str` | `COMPLETE`, `PARTIAL`, `BLOCKED`, `FAILED`, or `DEFERRED` (re-queued, no attempt consumed) |
| `digest: str \| None` | the artifact digest the lane computed; the loop re-verifies it |
| `artifacts: list[str]` | root-relative paths of the files the job produced |
| `notes: str` | free text for the owner |
| `model`, `session_id: str \| None` | what ran, when a lane knows it |
| `usage: dict` | measured usage, lane-defined |
| `error: str \| None` | why it failed |
| `raw: dict` | the lane's raw result, kept with the record |
| `reset_epoch: float \| None` | `DEFERRED` only: the earliest re-dispatch time |

### `job_spec`

`job_spec(task) -> dict`: a copy of the task's `job:` block (`{}` when it has none), what a lane's `prepare()` reads
to build its spec: `job: {adapter: render, scene: ...}` in the task contract. The task `prepare()` receives also has
`id`, the attempt's run folder name; extensions read nothing else from it.

### `CapacityDeferred`

`CapacityDeferred(reset_epoch, message)`, a `RuntimeError`: raised from `submit()` when nothing was launched because
capacity is out for now. The loop keeps the job prepared, records `message` and `reset_epoch` (epoch seconds, the
earliest time it is tried again) and counts no attempt, however often it happens.

## Agent lanes

### `agent_lane`

`agent_lane(layout, name) -> AgentLane`: a read-only handle on the registered agent lane `name` (a lane that runs an
agent CLI), for an extension that has to launch the agent itself in a way one job step cannot express, such as a
JSON-RPC session with the CLI or an exec that passes images. The extension passes the lane's name; the framework's
code names none. Raises `ExtensionError` for a name that is no lane, or a lane that runs no agent.

### `AgentLane`

| Member | Meaning |
|---|---|
| `name` | the lane's name |
| `executable` | the agent's executable as the lane resolves it (routing.yaml's `executable` for the lane's engine, else the one on `PATH` or the vendor's default install location) |
| `model` | the model routing.yaml configures for the lane's engine (`<engine>: {model: ...}`), or `None` |
| `environment() -> dict` | a fresh copy of the environment the lane gives its own agents: the loop's environment with every API key, token and nesting marker removed (`process.scrub_env()`: the variables whose names end in a `config/policy.yaml` `secrets.env_key_suffixes` entry, and the variables that would make the agent a nested session). Pass it as the child's whole environment, so the agent runs on its subscription login and never on a key. Changing the copy changes nothing else |

The handle is read-only: setting an attribute raises `AttributeError`. A launch through it is the extension's own
process, outside the loop's job records, capacity gate and timeouts; use a job lane (`Adapter`) for work the loop
should schedule and recover.

## Inbox items

An extension that claims an inbox kind with `registry.inbox_kind(kind, ...)` owns the items of that kind: it creates
them through its jobs, and only it changes or closes them (a kind claimed with `answerable=False` cannot be closed
from the inbox form or the `inbox.answer` command). Every operation below acts for the calling extension (the
configured extension whose code is running, see `config()`), raises `ExtensionError` when the caller is not an
extension's code, and raises `InboxError` when the item is missing or of a kind the caller did not claim. Items are
plain mappings as stored (`state/inbox/<id>.yaml`): `id`, `kind`, `status` (`open`, `answered`, ...), `subject`,
`summary`, the extension's own fields, and `answer` once closed.

### `owned_items`

`owned_items(layout, kind=None) -> list[dict]`: the items of every kind the caller claimed, or of `kind` (which it
must have claimed), as read now, by id.

### `owned_item`

`owned_item(layout, item_id) -> dict`: one item, as read now.

### `update_owned_item`

`update_owned_item(layout, item_id, fn) -> dict`: read-modify-write under the inbox lock (`state/.inbox.lock`, the
lock every inbox write takes), so concurrent updates of one item, from the loop, the Control Center and the CLI in
other processes, all land. `fn(doc)` receives the item as read under the lock, changes it in place or returns a new
mapping, and may raise to abort without writing anything. It may not change `id`, `kind`, `status`, `answer` or
`answered_at` (`InboxError`, nothing written): an update cannot turn an item into another kind or close it. Returns
the item as written. `fn` must not call another inbox operation (the lock is held).

### `close_owned_item`

`close_owned_item(layout, item_id, response, note="", by="owner", command_id=None) -> dict`: answer (close) the item,
the one path that closes an item of a kind claimed with `answerable=False`. `response` is the answer word or text,
checked against the item's `allowed_responses` unless the item allows free text. Idempotent by `command_id`: a
replay with the same id returns the item unchanged and logs nothing; closing an already closed item with another or
no id raises `InboxError`. Logged as `inbox_answer`.

### `InboxError`

A `ValueError`: an inbox item that does not exist, is not the caller's, or cannot take the change asked of it.

## Commands and the log

### `submit_command`

`submit_command(layout, kind, payload, actor="owner-ui", command_id=None) -> dict`: submit an owner command, the
way every Control Center action changes state: applied now when no loop holds the state lock, otherwise queued for
the loop, which applies it within one iteration. Returns the applied record (`{"id", "ok", "result"}` or
`{"id", "ok": False, "error"}`) or `{"id", "queued": True}`. The handler (registered with `registry.command`) gets
`payload` plus `_id`, the command id, and must be idempotent by it.

`command_id` makes a resubmission harmless, a form posted twice say: a command whose id was already submitted is not
applied again, and the answer is that command's record (or `queued`) with `"replayed": True`, whatever the second
`kind` and `payload` were. An id is letters, digits, `_`, `.` and `-`, at most 80 characters, not starting with `.`
or `-` (`ValueError` otherwise). Without `command_id` every call is a new command.

### `log_event`

`log_event(layout, event, actor=None, **fields) -> None`: one line in `state/log.jsonl` through the framework's single
log path: `{"ts", "event", "actor", ...fields}`, with credentials redacted from every field. The actor is the calling
extension's name, or `<extension>:<actor>` when `actor` is given (`review_rounds:owner-ui`), so every line says which
extension wrote it. Raises `ExtensionError` when the caller is not an extension's code.

## Policy

### `policy`

A module: an extension's own policy floor in `config/policy.yaml`, and the framework's policy diagnostics. Everything
it returns is a copy.

| Member | Meaning |
|---|---|
| `merge_floor(floor, configured) -> (dict, list[str])` | the framework's merge rule for an extension's floor: section by section, a scalar from `configured` wins, a list is unioned with the floor's, and an entry whose kind differs from the floor entry it would replace (a string for a flag, `null` for a list) is dropped and reported as text. Neither input is changed |
| `load(layout) -> dict` | the effective `config/policy.yaml`: the framework's floor merged with the file, sections the framework has no floor for (an extension's) as written |
| `section(layout, name) -> dict` | one section of `load()`, `{}` when absent or not a mapping |
| `config_problems(layout) -> list[str]` | the entries the framework rejected; each is a value the owner wrote that is not in effect |
| `secret_scan_problems(layout) -> list[str]` | why the credential store could not be screened completely; non-empty means every packet is refused |
| `PolicyViolation` | raised by a packet screen (`registry.packet_screen`) to refuse a brief; the loop refuses the job |

## Control Center pages

### `ui`

A module, loaded on first use (it needs the framework's `ui` extra, FastAPI): what an extension page registered with
`registry.ui_router` uses so it looks, authenticates and writes like the framework's own pages.

| Member | Meaning |
|---|---|
| `require_auth` | the page dependency: `layout: Layout = Depends(ui.require_auth)`; refuses a request from another host or origin and returns the project's `Layout` |
| `require_csrf(request, token)` | call first in every POST handler with the form's `csrf_token` field; a missing or wrong token is HTTP 403 |
| `render(request, template, context)` | a template response with the framework's context (`csrf_token`, `project_name`); a template `{% extends "base.html" %}` for the framework's chrome (registered with `registry.template_dir`) |
| `act(request, layout, kind, payload, redirect_to)` | submit an owner command as the Control Center does (actor `owner-ui`) and answer with the framework's banner: a fragment for an HTMX request, else a redirect to `redirect_to` with a flash |
| `flash(level, message) -> str` | the value of a `?flash=` query parameter for a redirect of your own (`applied`, `pending`, `error`) |
| `thumbnail(layout, path, size=(256, 256)) -> str \| None` | the key of a cached thumbnail of an image inside the project, served at `/thumb/<key>`; `None` for anything else |
| `is_image(path) -> bool` | whether the Control Center shows `path` as an image |

## Doctor checks

### `Check`

A dataclass `Check(name: str, ok: bool, detail: str, warn: bool = False)`: what a function registered with
`registry.doctor_check(fn)` returns, `fn(layout) -> Check`. `supervisor doctor` and the Control Center show it:
`ok=False` is a FAIL (doctor exits 1); `ok=True, warn=True` is a WARN, something to look at that fails nothing.

## The GPU probe

### `gpu`

The framework's NVIDIA memory probe (a module). `gpu.free_mb()`, `gpu.used_mb()`, `gpu.total_mb() -> int | None`
ask `nvidia-smi` for the first GPU's memory in MiB; `gpu.available() -> bool` is whether it answers at all. They never
raise: a missing driver, a timeout (10 s), an error exit or unreadable output is `None` (`False`). Nothing here
decides policy.

## Time

### `now_iso`

`now_iso() -> str`: the current UTC time as ISO 8601 to the second (`2026-10-27T09:30:00+00:00`), the format of
every timestamp the framework writes.

## The reference extension

`examples/reference_extension/reference/` is a working extension that uses every registry hook and every operation
above, through `supervisor.ext` only: a subprocess lane that defers when its config says so, a python job, a doctor
check that reports an agent lane, an Overview panel, a packet screen, a projection, a CLI group, two owner commands
over an inbox kind it owns, and a page with its templates and static files. Copy the folder to a project's
`extensions/reference/` and list `reference` in `project.yaml` to try it. `tests/test_hook_contracts.py` loads it
through the real loader and checks each hook's contract; a hook added to `Registry` without a check there fails.

## Testing extensions: `supervisor.testing` (unstable)

An extension's behaviour tests may import `supervisor.testing`, the one other part of `supervisor` an extension may
use. It is **explicitly unstable**: it can change in any release, minor ones included, G2 does not record it, and
nothing in it is promised. It is promoted from the framework's own test fixtures, which use it too.

| Helper | What it does |
|---|---|
| `project(root, *, name=None, extensions=None, modules=None, files=None, routing=None, policy=None) -> Layout` | a temporary project: state folders, phase 0 open, no owner tasks; `modules` writes `extensions/<name>.py` and lists them; `files`, `routing`, `policy` write files and config; forgets loaded extensions |
| `add_extensions(layout, modules, entries=None, files=None) -> Layout` | add extension modules to a project and list them in `project.yaml`, keeping its other keys |
| `make_task(layout, tid, ..., **fields) -> dict` | a minimal valid task (cost category `deterministic`); any task field as a keyword (`job=`, `artifacts=`) |
| `FakeLane(name, *, status="COMPLETE", artifacts=(), notes="", error=None, defer=0, defer_until=0.0, gpu=False)` | an in-process lane that writes `artifacts` and finishes with `status`, or defers the first `defer` submits; `submitted` and `deferred` record what it did |
| `lane(adapter)` | a context manager registering a lane for the block |
| `drive(layout, iterations=10, until=None)` | run the loop without sleeping until `until()` holds |
| `ui_client(layout, csrf=CSRF)` | a Control Center test client with the CSRF cookie set (imported on use; needs the `ui` and `dev` extras) |
| `import_side_effects(layout, name)` | what importing an extension module registers by itself, measured in a fresh interpreter |
| `reset()` | forget loaded extensions and what they registered |

Nothing in it launches a model client or reaches the network.
