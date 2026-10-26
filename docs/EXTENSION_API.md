# The public extension API (`supervisor.ext`)

An extension imports `supervisor.ext` and nothing else from `supervisor` (besides `supervisor.testing` in its tests,
which is explicitly unstable). Everything listed here is a promise: gate G2 (`python -m supervisor.api_contract
check`, `docs/BOUNDARY.md`) keeps `supervisor.ext.__all__` equal to the recorded contract in `docs/extension_api.json`
and compares it with the last release's. A name is removed, or its signature changed in a way that breaks a caller,
only in a major release, after a minor release that warns; an addition comes in a minor release with an
"Extension API" line in `CHANGELOG.md`. Anything not listed here is internal and may change in any release.

```python
from supervisor.ext import Adapter, Check, Layout, Result, atomic_json, gpu
```

The API is introduced in 1.11.0. Where a name is the framework's own object, re-exported unchanged, it says so: the
behaviour described is the framework's, and a test reaches it through `supervisor.ext` (`tests/test_ext_reexports.py`).

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

### `CapacityDeferred`

`CapacityDeferred(reset_epoch, message)`, a `RuntimeError`: raised from `submit()` when nothing was launched because
capacity is out for now. The loop keeps the job prepared, records `message` and `reset_epoch` (epoch seconds, the
earliest time it is tried again) and counts no attempt, however often it happens.

## Doctor checks

### `Check`

A dataclass `Check(name: str, ok: bool, detail: str)`: what a function registered with `registry.doctor_check(fn)`
returns, `fn(layout) -> Check`. `supervisor doctor` and the Control Center show it; `ok=False` is a FAIL.

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
