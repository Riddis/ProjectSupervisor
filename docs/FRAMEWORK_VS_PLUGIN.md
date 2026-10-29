# Framework vs. project: where a change goes

The rule of thumb: **would a project about something completely different still want this?** Yes → framework.
No → the project (`plugins/` for code, `config/`/`agents/`/`docs/` for everything else).

## The boundary

| | Framework (`Supervisor` repo) | Project folder |
|---|---|---|
| Code | `supervisor/` package: loop, store, gates, review, verification, inbox, checkpoints, policy, generic adapters, Control Center | `plugins/*.py` — adapters for the project's own tools, extra doctor checks, UI panels/pages, packet screens, projections, CLI groups, python jobs |
| Config | defaults and schemas | `project.yaml`, `config/routing.yaml` (roles, models, limits, test commands), `config/policy.yaml` (plus any project section) |
| Roles | the generic card set in `supervisor/template/agents/` | `agents/` — the project's actual roles and result schemas |
| Process | the task lifecycle, gate semantics, review and escalation rules | `state/gates.yaml` (which gates open which phase), `docs/plan/PROJECT_PLAN.md`, decision records |
| Docs | how the framework works, runbooks for the generic lanes | the plan, project runbooks, research |

The framework never imports from a project. A project never edits framework files; it configures and extends.
`docs/BOUNDARY.md` has the layer model, the rules for hard cases and the gates that check them: the boundary check
(`python -m supervisor.boundary`, G1) fails a framework change that imports project code, carries a path, host or
person, or names a vendor outside the listed files.

## Plugins

A plugin imports one thing from the framework, the public plugin API `supervisor.plugin`, whose every name,
signature and behaviour is in `docs/PLUGIN_API.md`; `examples/reference_plugin/` uses all of it.

`project.yaml` lists plugins under `plugins:` by identity. Each is one of these:
- a project-local module `plugins/<name>.py` (or package folder);
- an installed package that declares an entry point in the group `supervisor.plugins`, such as the shared
  plugin packages from 1.12.0.

Either way its manifest, `supervisor-plugin.toml`, declares what it is, which framework versions it supports and
what it registers (`docs/PLUGIN_STANDARD.md`, 1.12.0). The manifest is read before anything is imported, and
`setup(registry)` is called once per process (by the CLI, the loop and the Control Center alike), all or nothing.

`supervisor plugin new <name>` scaffolds a project-local one with its manifest and lists it. `--package` creates
the standard package instead, and `supervisor plugin check <name>` (gate G5) proves either conforms.

```yaml
plugins:
  - render_farm                        # required (the default): if it fails to load, the loop refuses to run
  - {name: nightly_report, required: false}   # optional: a load failure is only a doctor warning
```

- **Public API, setup() only.** A plugin imports `supervisor.plugin` (the public plugin API,
  `docs/PLUGIN_API.md`) and registers everything inside `setup(registry)`. Importing its modules must change
  nothing: no lane, python job or command registered at import, no process started.
  `plugins.import_side_effects(layout, name)` checks this in a fresh interpreter. Module-level `register()` and
  `@job` still work (1.11.0) but are deprecated, as is any other import from `supervisor`: the loader warns, and
  `supervisor doctor` shows a WARN (`plugin_imports`) with each import's file and line.
- **Isolation.** The `plugins/` directory is loaded by file path as its own package (`supervisor_ext.<root hash>`);
  it is never put on `sys.path`. Two projects' `shared_ext.py` are two different modules, and a plugin named
  `json` is the project's file, not the stdlib. Modules in one project's `plugins/` import each other with
  relative imports (`from . import helpers`). Names must be identifiers (dotted for sub-packages).
- **Failure.** A configured plugin that does not import, or has no `setup(registry)`, is recorded and shown by
  `supervisor doctor` (`plugins`). Unless the entry says `required: false`, it also blocks: `supervisor run`
  exits 1 with runtime status `BLOCKED` and a reason naming the plugin, and `policy.assert_packet_clean()` refuses
  every packet — a missing packet screen or lane is a missing protection, not a degraded mode.
- **Lane availability (adapters).** Two optional methods (1.10.0) let a lane that can be out of service as a whole say so: `submit()`
  raises `LaneUnavailable` (no attempt is spent; a delivery the scheduler moved there goes back to its own lane),
  `lane_ready(layout)` releases the records it deferred early, and `reset_lane(layout)` backs the owner command
  `lane.reset`. Both default to "no", so existing adapters are unaffected.
- **Adapters.** An exception from a plugin adapter's `prepare()`, `recover()`, `submit()` or `collect()` fails
  that one attempt (job record `FAILED` with the exception text, event `adapter_error`, the task's verification
  detail carries it) and the loop goes on; it does not count against the loop's own error budget.

```python
# plugins/render_farm.py
from supervisor.plugin import Check, ProcessAdapter

class RenderAdapter(ProcessAdapter):
    name = "render"
    gpu = True                       # holds the project's single heavy-resource lock (limits.max_resource_jobs)

    def prepare(self, layout, task, attempt_id):
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
                "root": str(layout.root), "argv": ["render", "--scene", task.model_extra["job"]["scene"]],
                "timeout_s": 1800}

def setup(registry):
    registry.adapter(RenderAdapter())
    registry.doctor_check(lambda layout: Check("render", True, "renderer found"))
    registry.overview_panel(lambda layout: {"title": "Render queue", "rows": [("waiting", "3")]})
```

Then in `config/routing.yaml`: `cost_lanes: {local: render}` or, per task, `job: {adapter: render, scene: ...}`.

What the registry offers (`supervisor/plugins.py`):

| Hook | Use it for |
|---|---|
| `adapter(adapter)` | a new job lane. Subclass `supervisor.plugin.ProcessAdapter` for anything that is a subprocess (it gives you crash-safe handles, timeouts, `result.json` collection); subclass `supervisor.plugin.Adapter` directly for in-process or HTTP work. A name another lane already has (built in or another plugin's) is refused (1.11.0). |
| `doctor_check(fn)` | `fn(layout) -> Check(name, ok, detail)`; shown by `supervisor doctor` and the Control Center's doctor button. |
| `overview_panel(fn)` | `fn(layout) -> {"title", "rows": [(label, value)], "progress": (value, max)?}`; a resource block on the Overview. |
| `ui_router(router, label, path)` | a FastAPI `APIRouter` (use `supervisor.plugin.ui`: `require_auth`, `require_csrf`, `render`, `act`, so your page looks, authenticates and writes like the others) plus a sidebar entry. |
| `packet_screen(fn)` | `fn(layout, text, where, *, job=None, task=None, lane=None)` raising `policy.PolicyViolation` to refuse a brief. Runs after the credential screen on every packet. The hosted lanes (`claude_cli`, `codex_cli`, `claude_api`) pass the task's `job` block, the Task and their lane name, so a screen can refuse on *what the job is* (a channel whose content may not leave the machine, a measured content class), not only on the prompt text; declare only the keyword arguments you use — `fn(layout, text, where)` still works and sees text only. |
| `projection(fn, path=None, name=None)` | `fn(layout) -> Path`; an extra rendered markdown file alongside `TASKS.md`. Like the built-in views it is never an artifact and never an "undeclared write" in verification; pass `path="WEEKLY.md"` (root-relative) to declare the file before the first render, otherwise it is learned from the first render. The exclusion follows the current registration: change the declared path, stop registering the projection, or remove the plugin, and the old file is an ordinary file again. Each projection is recorded as `<plugin>:<name>` — pass `name=` for a stable identity (two lambdas or two callbacks from one factory are otherwise told apart by registration order). The callback runs under `state/.render.lock`, like the built-in views (1.10.0): it may call a built-in `render_*` function (the lock is reentrant within a thread), but must not wait for another thread or process that renders. |
| `cli(typer_app, name)` | a `supervisor <name> ...` command group. Looked up when `<name>` is not a built-in command, or when the whole command list is wanted (`supervisor --help`, top-level shell completion) (1.10.0): resolving a built-in command never sets the plugins up, so the read-only `supervisor status` runs without them; running another built-in command may load them. Kept per project root, rebuilt when that root's registry is reloaded. |
| `python_job(name, fn)` | a deterministic job for `job: {adapter: python, fn: name, params: {...}}`; `fn(root, run_dir, **params) -> {"status", "artifacts", "notes"}`. A taken name is refused (1.11.0). |
| `command(kind, fn)` | an owner command kind (`my_ext.submit`) applied by whoever holds the state lock — loop, Control Center, CLI — exactly like the built-in kinds: `fn(layout, payload, actor) -> dict`, idempotent by `payload["_id"]`. A kind that already exists is an error, never an overwrite. |
| `inbox_kind(kind, label, page=None, answerable=False, show_evidence=True, score_keys=())` | claim an inbox item kind your plugin creates. `label` heads its group on the inbox page; `page` (`"/review/{id}"`) is linked from the card; `answerable=False` means the generic answer path (inbox form, `inbox.answer` command, `inbox.answer()`) refuses to close the item and only your code may, with `supervisor.plugin.close_owned_item()` (1.11.0; it and `owned_items`, `owned_item`, `update_owned_item` act only on kinds your plugin claimed); `show_evidence=False` keeps the per-option rows and their scores off the card (blind labelling — the owner sees a count and the link); `score_keys` names the sub-item fields that make a row a scored one. Built-in kinds (`gate`, `decision`, `approval`, `spend`, `policy`, `question`) cannot be claimed. |
| `template_dir(path)` | Jinja templates for your pages. Loaded after the framework's, so a page can `{% extends "base.html" %}` and include the partials; a framework template is never overridden by a same-named file of yours. |
| `static_dir(path, prefix)` | files served at `/static/<prefix>/...` (your `review.js`, a stylesheet, icons). One path segment per prefix; the framework's own assets stay directly under `/static/`. |

Plugins may read their own section of `config/policy.yaml` through `supervisor.policy.section("my_section")`
and their own configuration through `supervisor.plugin.config(layout, <identity>)`. From 1.12.0 that is
`plugins.<identity>` in `config/routing.yaml`, validated by the schema the manifest names. A plugin without a
manifest reads the sections it declares with `registry.config_sections(...)`. They read the shared resource policy
(`resources.gpu`) through `supervisor.plugin.resources(layout)` (`docs/PLUGIN_API.md`); `supervisor migrate` moves
the pre-1.12.0 top-level sections there. `supervisor.plugin.gpu` (`free_mb`, `used_mb`,
`total_mb`, `available`) is the shared nvidia-smi probe for lanes, checks and panels that need to know what the card
has free; it never raises.

The single heavy resource (`limits.max_resource_jobs`, default 1) is held by any adapter declared `gpu = True` and,
when routing.yaml lists `limits.resource_cost_categories: [local]`, by the *delivery* of a task of that cost category
on any lane — an agent session installing into, starting or benchmarking the resource holds the slot like the lane
that renders on it, so two such jobs never share it.

## Promoting a plugin into the framework

When something written for one project turns out to be generic — a lane for a tool many projects use, a better
diagnosis rule, a Control Center improvement — move it into `supervisor/` deliberately: port the code and its tests,
add a `CHANGELOG.md` entry, bump the version, and (if it changes state or config shape) add a migration. The project
then drops its copy. Nothing syncs automatically in either direction, by design: the editable install means a
framework fix is live in every project the moment it is saved, so framework edits should be made as framework
commits, not prototyped in the framework from inside a project session.

## Things that look project-specific but are framework

- A bug in the loop, verification, review or inbox logic — fix it in the framework, with a test.
- A new generic adapter (another agent CLI, a generic HTTP job runner) — framework.
- Control Center behaviour every project would want (a filter, a keyboard shortcut, a clearer status) — framework.
- Role *mechanics* (how cards are prepended, how reviews are confined) — framework. Role *content* — project.

## Things that look generic but are project

- Which roles exist, their cards, their models — project (`config/routing.yaml`, `agents/`).
- Phase labels, gates, what evidence a gate needs — project.
- The allowed test commands (`verification.allowed_test_commands`) — project; the framework ships a Python default.
- Extra Bash permissions an agent needs (`claude.bash_allow`) — project.
- Extra folders an agent session may work in (role `add_dirs`, `claude.add_dirs_by_cost`) — project.
