# Framework vs. project: where a change goes

The rule of thumb: **would a project about something completely different still want this?** Yes → framework.
No → the project (`extensions/` for code, `config/`/`agents/`/`docs/` for everything else).

## The boundary

| | Framework (`Supervisor` repo) | Project folder |
|---|---|---|
| Code | `supervisor/` package: loop, store, gates, review, verification, inbox, checkpoints, policy, generic adapters, Control Center | `extensions/*.py` — adapters for the project's own tools, extra doctor checks, UI panels/pages, packet screens, projections, CLI groups, python jobs |
| Config | defaults and schemas | `project.yaml`, `config/routing.yaml` (roles, models, limits, test commands), `config/policy.yaml` (plus any project section) |
| Roles | the generic card set in `supervisor/template/agents/` | `agents/` — the project's actual roles and result schemas |
| Process | the task lifecycle, gate semantics, review and escalation rules | `state/gates.yaml` (which gates open which phase), `docs/plan/PROJECT_PLAN.md`, decision records |
| Docs | how the framework works, runbooks for the generic lanes | the plan, project runbooks, research |

The framework never imports from a project. A project never edits framework files; it configures and extends.

## Extensions

`project.yaml` lists modules under `extensions:`; each is loaded from the project's `extensions/<name>.py` (or, when
the project has no such file, imported as an installed dotted module) and its `setup(registry)` is called once per
process — by the CLI, the loop and the Control Center alike. `supervisor extension new <name>` scaffolds one and
registers it.

```yaml
extensions:
  - render_farm                        # required (the default): if it fails to load, the loop refuses to run
  - {name: nightly_report, required: false}   # optional: a load failure is only a doctor warning
```

- **Isolation.** The `extensions/` directory is loaded by file path as its own package (`supervisor_ext.<root hash>`);
  it is never put on `sys.path`. Two projects' `shared_ext.py` are two different modules, and an extension named
  `json` is the project's file, not the stdlib. Modules in one project's `extensions/` import each other with
  relative imports (`from . import helpers`). Names must be identifiers (dotted for sub-packages).
- **Failure.** A configured extension that does not import, or has no `setup(registry)`, is recorded and shown by
  `supervisor doctor` (`extensions`). Unless the entry says `required: false`, it also blocks: `supervisor run`
  exits 1 with runtime status `BLOCKED` and a reason naming the extension, and `policy.assert_packet_clean()` refuses
  every packet — a missing packet screen or lane is a missing protection, not a degraded mode.
- **Adapters.** An exception from an extension adapter's `prepare()`, `recover()`, `submit()` or `collect()` fails
  that one attempt (job record `FAILED` with the exception text, event `adapter_error`, the task's verification
  detail carries it) and the loop goes on; it does not count against the loop's own error budget.

```python
# extensions/render_farm.py
from supervisor.adapters.process import ProcessAdapter
from supervisor.doctor import Check

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

What the registry offers (`supervisor/extensions.py`):

| Hook | Use it for |
|---|---|
| `adapter(adapter)` | a new job lane. Subclass `supervisor.adapters.process.ProcessAdapter` for anything that is a subprocess (it gives you crash-safe handles, timeouts, `result.json` collection); subclass `Adapter` directly for in-process or HTTP work. |
| `doctor_check(fn)` | `fn(layout) -> Check(name, ok, detail)`; shown by `supervisor doctor` and the Control Center's doctor button. |
| `overview_panel(fn)` | `fn(layout) -> {"title", "rows": [(label, value)], "progress": (value, max)?}`; a resource block on the Overview. |
| `ui_router(router, label, path)` | a FastAPI `APIRouter` (use `supervisor.ui.deps.render` and `act` so your page looks and writes like the others) plus a sidebar entry. |
| `packet_screen(fn)` | `fn(layout, text, where)` raising `policy.PolicyViolation` to refuse a brief — content that must not leave the machine, say. Runs after the credential screen on every packet. |
| `projection(fn, path=None)` | `fn(layout) -> Path`; an extra rendered markdown file alongside `TASKS.md`. Like the built-in views it is never an artifact and never an "undeclared write" in verification; pass `path="WEEKLY.md"` (root-relative) to declare the file before the first render, otherwise it is learned from the first render. |
| `cli(typer_app, name)` | a `supervisor <name> ...` command group. |
| `python_job(name, fn)` | a deterministic job for `job: {adapter: python, fn: name, params: {...}}`; `fn(root, run_dir, **params) -> {"status", "artifacts", "notes"}`. |

Extensions may read their own section of `config/policy.yaml` through `supervisor.policy.section("my_section")`
and their own keys in `config/routing.yaml` (`supervisor.adapters.claude_cli.routing(layout)`).

## Promoting an extension into the framework

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
