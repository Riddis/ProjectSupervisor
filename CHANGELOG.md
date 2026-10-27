# Changelog

All notable changes to the framework. Versions follow semver: patch = fix, minor = feature or new extension hook,
major = a change that needs `supervisor migrate` (and says so under **Migration**).

## 1.12.0 — 2026-10-28

Phase 3 of the boundary plan (docs/BOUNDARY.md): the extension standard (docs/EXTENSION_STANDARD.md).
- **Manifests:** every extension has a manifest, `supervisor-extension.toml`, read before anything it ships is
  imported.
- **Installed packages:** they are found through the entry point group `supervisor.extensions`.
- **Loading** is approved first (framework range, collisions, requirements), then all or nothing.
- **Configuration:** an extension owns `extensions.<identity>`, validated by its schema; the shared resource policy
  is `resources.gpu`.
- **Gate G5,** `supervisor extension check`, proves an extension conforms, and `extension new --package` scaffolds
  the standard package.

Also: the update check on a pinned framework checkout, carried from the unreleased 1.11.1. A minor release:
everything that works on 1.11.0 keeps working, with warnings (owner decision D1).

**Migration:** `supervisor migrate` moves configuration to its owners, keeping comments and every other line of
`config/routing.yaml` as written:
- `gpu` to `resources.gpu`;
- each configured extension's manifest's `config.legacy_sections` into `extensions.<identity>`.

Then the migrations extension manifests name run. Its dry run lists each move. Running it is not required: the old
sections are still read, with a DeprecationWarning and a doctor WARN. The update-check cache gains fields that 1.11.0
discards.

**Rollback** to v1.11.0, in a maintenance window: restore the snapshot of `config/`, `state/` and `project.yaml`
taken before `supervisor migrate`. v1.11.0 does not read `extensions.<identity>` or `resources.gpu`. Then check out
`v1.11.0`, run `supervisor migrate` (it records 1.11.0 again) and `supervisor doctor`, and start. Manifests are
ignored by 1.11.0. An extension that calls `registry.identity` does not load there. Once work has run on 1.12.0,
reconcile first (rollback B).

**Extension authors:**
- Add a manifest beside your module (`extensions/<name>.toml`) or in your package. It declares everything `setup()`
  registers, with the identifiers older than the namespace rule as `legacy_ids`.
- Name a pydantic schema for your section, and list the old top-level sections as `config.legacy_sections`.
- Run `supervisor extension check <name>`.

An extension without a manifest still loads in 1.12.x, with a doctor WARN; a later major release requires one.

**Review:** the G4 review covers `v1.11.0..` this release commit, including the carried pinned-checkout commits.
Astra reviewed `v1.11.0..a212283` in two rounds, and every finding is fixed. `a212283..a90dc13` (round 2's R2-1 and
R2-2, and the fetch-the-tag step) has not been reviewed yet.

- **Added: the extension manifest** (`supervisor/manifest.py`, `docs/EXTENSION_STANDARD.md`).
  `supervisor-extension.toml` declares an extension's identity, its version, the framework versions it supports,
  what it registers (`provides`, with `legacy_ids`), its configuration section and schema, what it requires, and
  its migrations.
  - It is read without importing anything the extension ships.
  - Parsing is strict, and every problem is reported at once.
  - The framework range is compared by version number.
  - The loader does not read manifests yet.
  - New dependency: `packaging` (version ranges, PEP 440), already present wherever pip is.
- **Added: discovery without import** (`supervisor/discovery.py`). Each name in `project.yaml: extensions` is found
  with its manifest, executing nothing the extension ships:
  - project-local, beside the module or inside the package folder;
  - installed, through the entry point group `supervisor.extensions` and the distribution's file list, or, for an
    editable install, beside the package `find_spec` locates.

  The same name from two sources is refused, and so is a manifest naming another identity. An extension without a
  manifest is found as legacy.
- **Changed: loading is all or nothing** (`supervisor/extensions.py`). `setup()` registers into a staging area, and
  the lanes, python jobs and command kinds reach the process-wide tables only in one commit after it returns.
  Nothing an extension registered remains, required or optional, when any of these happens:
  - `setup()` raises;
  - `setup()` registers something its manifest does not declare;
  - discovery refuses its manifest;
  - its import fails.

  The project's registry is put back exactly, and the module is dropped from `sys.modules`. Before, whatever
  `setup()` registered before failing stayed. The loader now finds each extension through discovery: the same name
  from two sources is refused, where a project-local file used to win silently. `registry.manifests` and
  `registry.unmanifested` list who has a manifest.
- **Added: approval before import.** From the manifests alone, before any extension module is imported, the loader
  refuses:
  - an extension whose framework range excludes the installed framework;
  - a manifest claiming a built-in identifier;
  - an identifier two manifests claim (both are refused);
  - a `requires.extensions` entry `project.yaml` does not list, or a cycle.

  Extensions load after the ones they require, and one whose requirement did not load is refused. Refusals are load
  failures: a required one blocks the loop.
- **Extension API: `Registry.identity`.** During `setup()`, `registry.identity` is the identity of the extension
  being set up: its manifest's `identity`, which discovery holds equal to the name `project.yaml` uses. An
  extension builds its namespaced identifiers from it. The `Registry` section of `docs/EXTENSION_API.md` gains
  the attribute; this is a compatible addition.
- **Extension API: `config` and `resources`, configuration ownership.** An extension whose manifest has a `config`
  table owns `extensions.<identity>` in `routing.yaml`, validated by its pydantic model before `setup()` runs.
  Invalid configuration is a load failure that leaves nothing registered. `config(layout, <identity>)` is the
  validated section, and `config(layout, <legacy section>)` a part of it. Until `supervisor migrate` moves
  them, values are read from the old top-level sections in `config.legacy_sections` (new manifest field),
  with a DeprecationWarning.

  `resources()` reads `resources.gpu`, else the old `gpu` (warned), and an extension with a manifest sees
  only what it declares in `requires.resources`. Extensions without a manifest see no change, and the
  signatures are unchanged.
- **Added: the configuration move and extension migrations** (`supervisor/migrations.py`, `supervisor/yaml_edit.py`).
  `supervisor migrate` moves routing.yaml's `gpu` to `resources.gpu`, and each configured extension's
  `config.legacy_sections` into `extensions.<identity>`: the section named like the identity at the top, any other
  nested under its own name. Both are driven by the manifests discovery reads, so no extension code runs.
  - `yaml_edit.move_sections` cuts each section's lines and re-indents them under the destination. Comments move
    with the lines, the one on the key line included, and every other line stays byte for byte, in the file's own
    line endings.
  - Where lines cannot be moved (a flow-style parent), a comment-keeping round trip is used instead.
  - Either way the result must read back as the same values moved, or nothing is written.

  Then the migrations each manifest names (`migrations = "<module>"`, exposing `MIGRATIONS`) run, importing only
  that module, never `setup()`. Rollback: the maintenance window's snapshot. v1.11.0 does not read the new places.
- **Changed: doctor and migrate do not depend on loading.** `supervisor migrate` no longer sets extensions up.
  `supervisor doctor` gains the check `extension_manifests` (G6), read from the manifests alone:
  - FAIL for a refused manifest or an excluded framework range;
  - WARN for an extension without a manifest, or for configuration not yet moved.

  An extension's doctor check must report a name its manifest declares in `provides.doctor_checks`.
- **Added: gate G5, `supervisor extension check <name>`** (`supervisor/extension_check.py`). It checks whether
  one extension conforms to the standard:
  - its manifest;
  - its framework range;
  - imports of `supervisor.ext` only;
  - no registration at import (in a fresh interpreter);
  - `setup()` registering exactly what `provides` declares, the same on a second run, against a staging registry
    never committed;
  - its configuration against the schema.

  It never runs the extension's tests.
- **Added: `supervisor extension new <name> --package`**, the standard extension package from
  `supervisor/extension_template/`. It holds `pyproject.toml` with the entry point, the package with its manifest
  and a pydantic config model, a generated conformance test, a behaviour test, a README and a CHANGELOG. It passes
  G5 and its own suite as generated.
- **Changed: `supervisor extension new <name>`** writes the manifest beside the module, and accepts only an identity
  the manifest accepts (lower case, digits, `_`).
- **Changed: the reference extension is a package** (`examples/reference_extension/`). It gains a manifest, a
  pydantic schema for `extensions.reference` and a `pyproject.toml` (entry point `reference`). Its projection is
  named `reference_notes`, so every identifier it registers is namespaced. It passes G5.
- **Added: CI job "G5 reference extension"** (`tools/ci_installed_extension.py`). In a fresh environment with the
  framework built rather than editable, it installs the reference extension as a wheel, with its source moved out
  of reach, and then as an editable install. Each time it shows that:
  - the extension is found through its entry point;
  - it loads, and passes `supervisor extension check`;
  - it completes a task on its lane.

  A package generated by `supervisor extension new --package` runs its complete suite. It is a required check from
  this release.
- **Fixed: an installed extension is imported by its entry point's module.** Discovery reports the module the entry
  point names (`supervisor_ext_notes` for identity `notes`), and the loader, G5 and the import-side-effect probe
  import that. Before, they imported the identity as a module name.
- **Fixed: the update check on a pinned checkout** (`supervisor/updates.py`). A live project runs a framework
  worktree detached at a release tag (docs/UPDATING.md, "Pinned checkouts"). The check treated detached `HEAD` as a
  branch named `HEAD` and compared it with `origin/HEAD`, which a worktree of a development clone does not have.
  Every launch printed "update check skipped: origin/HEAD unknown: fatal: ambiguous argument…", and nothing
  reported a newer release.
  - A detached checkout is now *pinned*. Both releases come from origin's own tag list (`git ls-remote --tags`,
    each tag peeled to its commit) and are compared by version number (v1.10.0 is above v1.9.0):
    - the release it runs is the newest one origin publishes at its commit;
    - the newer release is the newest one origin publishes.

    Other tag names are not releases, and neither is a tag that exists only locally, wherever it points. When
    origin publishes no release at the commit, the check says so and announces nothing. It fetches only the newer
    release's objects, writing no local ref, and says whether its changelog mentions a migration.
  - Without a fetch (the overview), the last result from origin is shown, with the time of that check. A failed
    refresh does not erase it: the overview adds "the latest refresh failed (<reason>)".
  - The launch check says nothing when the checkout is pinned at the newest release. It names a newer release and
    the maintenance-window steps, `git fetch origin tag vX.Y.Z` included, and never offers a pull.
  - The Control Center's overview shows "Framework release available" with no Update button.
  - `apply` (`supervisor update`, `/overview/update`) inspects the checkout itself. It refuses a detached checkout,
    and a status that no longer describes the checkout (another branch or commit), whatever status it is given, and
    changes nothing. A cached check is reused only while the checkout is still at the branch and commit it was taken
    of.
  - A detached checkout not at a release tag is reported plainly; `supervisor doctor` already fails it.
  - docs/UPDATING.md and the framework-change skill add `git fetch origin tag vX.Y.Z` before the checkout, since the
    check no longer creates the tag locally.

## 1.11.0 — 2026-10-27

Phase 2 of the boundary plan (docs/BOUNDARY.md): the public extension API `supervisor.ext`, gate G2 recording its
contract, the deprecation of internal imports in extensions, a reference extension with hook contract tests, and the
unstable `supervisor.testing`. Also: capacity floors that take effect (Settings offers the 7-day floor, a change
applies without a restart, a held job names its window and setting), and YAML writers that change only what they
edit. A minor release: everything added is new surface, and existing extensions keep working, with a doctor WARN for
each import of a framework internal.

**Migration:** none added; `supervisor migrate` records the new version and now changes only project.yaml's
`framework_version` line. Its dry run on a project at 1.10.1 lists only `project.yaml: framework_version 1.10.1 →
1.11.0`. No state or config shape change: `capacity.<engine>.weekly_gate_percent` already existed (Settings now edits
it), and `registry.config_sections()` declares sections routing.yaml already has.

**Rollback** to v1.10.1, in a maintenance window: drain, stop, check out `v1.10.1`, `supervisor migrate` (records
1.10.1 again), `supervisor doctor`, start. Nothing 1.11.0 writes is new to 1.10.1. An extension already moved to
`supervisor.ext` does not load on 1.10.1 (the package does not exist there), so roll its project change back with
the framework: for the Studio, the parent of its phase-2 merge.

**Extension authors:** import `supervisor.ext` only (docs/EXTENSION_API.md; examples/reference_extension/ uses all
of it) and register everything in `setup()`. Imports of framework internals and module-level `register()` / `@job`
keep working in 1.11.x with a DeprecationWarning and a doctor WARN; a later major release refuses them. Write tests
against `supervisor.testing`, which promises nothing.

- **Gate G2: the extension API contract** (`supervisor/api_contract.py`). `python -m supervisor.api_contract record`
  writes `docs/extension_api.json` from `supervisor.ext.__all__`: every public name with its kind and signature
  (parameter names, kinds, defaults and annotations; for a class its constructor, public bases, methods, properties
  and fields). `python -m supervisor.api_contract check` fails when that file does not describe the code, when a
  public name has no section of its own in `docs/EXTENSION_API.md`, and when the difference from the last release
  tag's contract breaks the version rule. Both halves are compared with the tag's (`git show <last v* tag>:...`):
  the signatures, where a removal or a change that breaks a caller (a removed or renamed parameter, a new required
  one, a lost default, a moved positional, a positional-only name `**kwargs` used to collect, a keyword-only
  parameter moved where an old positional argument lands) needs a major version
  and an addition or a compatible change at least a minor version with an "Extension API" entry naming it; and each
  name's documented section, where a change must be named in an "Extension API" entry marked `(editorial)` (any
  version), `(breaking)` (major) or neither (compatible: at least minor), since no program can prove prose
  compatible. A shallow clone fails instead of passing unchecked. CI runs it as the job `G2 extension API`, with the
  full history; 1.11.0 records the first contract, so the comparison starts with the release after it.
- **Extension API:** the package `supervisor.ext`, documented in `docs/EXTENSION_API.md`; its `__all__` is the
  contract. Re-exported unchanged, with tests through `supervisor.ext` for the behaviour extensions rely on: `Layout`
  (paths from the project root), the file helpers `atomic_write_text`, `atomic_json`, `read_json` and `contained`
  (atomic replace that waits out a reader on Windows; a path outside the root is not contained), the lane classes
  `Adapter`, `ProcessAdapter` and `Result`, `CapacityDeferred` (a deferral costs no attempt), the doctor's `Check`,
  the GPU probe `gpu` (never raises) and `now_iso`. `Layout`'s paths and `CapacityDeferred.reset_epoch` are now
  declared on their classes, so the contract records them; nothing else about them changes.
- **Extension API:** `Registry`, the hooks `setup(registry)` receives, as a documented `typing.Protocol`; the
  loader's registry keeps exactly its signatures (tested), so G2 now covers the hook signatures too.
- **Extension API: the config facade.** `registry.config_sections(*names)` in `setup()` declares the
  `config/routing.yaml` sections an extension reads; `config(layout, section)` returns one of them and
  `resources(layout)` the shared resource policy (`{"gpu": ...}`, routing.yaml's `gpu`), both as copies parsed for
  the call. `config()` refuses a caller that is not an extension's code and a section the calling extension did not
  declare; `config_sections()` refuses a framework section (the template routing.yaml's keys, `gpu`, `resources`,
  `extensions`) and one another extension declared. `ExtensionError` is exported for both refusals. This replaces
  reading routing.yaml through the claude_cli adapter's `routing()`; phase 3 keeps these signatures and moves the
  values to `extensions.<identity>` and `resources.gpu`. No config shape change.
- **Extension API: purpose-named operations** for what extensions reached private helpers for, each acting for the
  calling extension and tested for the behaviour that must survive:
  - inbox items of a kind the extension claimed: `owned_items`, `owned_item`, `update_owned_item` (read-modify-write
    under `state/.inbox.lock`, so concurrent updates from two processes both land; it cannot change the item's id,
    kind, status or answer) and `close_owned_item` (idempotent by command id); another extension's item is refused
    (`InboxError`, exported);
  - `submit_command(layout, kind, payload, actor, command_id=None)`: an owner command, applied now or queued for
    the loop; with a `command_id`, a resubmission is applied once and reported as `"replayed"`
    (`commands.submit` and `submit_or_apply` take the optional `command_id`);
  - `log_event(layout, event, actor=None, **fields)`: the framework's log path, the actor prefixed with the
    extension's name (`<extension>:<actor>`);
  - `policy`: `merge_floor` (the framework's merge rule for an extension's floor: lists unioned, a wrong-kinded
    entry dropped and reported), `load` and `section` as copies, `config_problems`, `secret_scan_problems`,
    `PolicyViolation`;
  - `ui`, loaded on first use: `require_auth`, `require_csrf`, `render`, `act`, `flash`, `thumbnail`, `is_image`;
  - `agent_lane(layout, name) -> AgentLane`: a read-only handle on an agent lane (owner decision 2026-10-27,
    option B) with `executable`, `model` and `environment()`, the lane's scrubbed environment; no credential that
    `process.scrub_env()` strips reaches a child launched with it. The two agent CLI lanes serve it through a new
    internal `agent_handle(layout)` method; `codex_env()` takes an optional layout.
- **Extension API:** `job_spec(task)`, the copy of a task's `job:` block a lane's `prepare()` reads (the framework's
  own helper; two Studio lanes kept copies of it).
- **Registration only in `setup()`.** `registry.adapter()` and `registry.python_job()` are how an extension adds a
  lane or a python job, and importing an extension module should change nothing. Both now refuse a name another lane
  or job already has, built in or another extension's (before, an extension could silently replace the `process` or
  `python` lane, or another extension's); registering the same object again is still harmless, so an extension that
  also registers at import keeps working. The registry lists what each extension registered (`adapters`,
  `python_jobs`), and `extensions.import_side_effects(layout, name)` imports a module in a fresh interpreter and
  names every lane, python job and command kind the import itself registered. Module-level `register()` and `@job`
  keep working and are deprecated (the loader warns, next entry). `supervisor extension new` writes a module
  that imports `supervisor.ext`. A name stays the registering extension's until the loader is reset, so the same
  extension may register it again after a reload; an extension is its source file, so one file configured under two names
  (a project's own tests import `extensions.comfy`, the loader `comfy`) is one extension. Tests:
  `extensions.reset()` keeps registered lanes and python jobs, as before (a project's suite looks them up after a reset); `supervisor.testing.reset()` forgets them too.
- **Internal imports in extensions are deprecated (gate G6).** After loading, the loader reads the source (AST) of
  every module in the project's `extensions/` folder, helpers included, and of installed extension modules, for
  imports from `supervisor` other than `supervisor.ext` and `supervisor.testing`. Each one gets a `DeprecationWarning`
  at its own file and line (once per process) and is listed by the new doctor check `extension_imports` as a WARN;
  the extension still loads and works. A later major release refuses them. Owners: expect this WARN for extensions
  not yet moved to `supervisor.ext`; it fails nothing.
- **Doctor has a WARN level.** `Check` gains `warn: bool = False` (**Extension API**: `Check.warn`): `ok=True,
  warn=True` prints `WARN` in `supervisor doctor` and the Control Center and does not change doctor's exit code.
- **The reference extension** (`examples/reference_extension/reference/`): every registry hook and every
  `supervisor.ext` operation, used through `supervisor.ext` only. `tests/test_hook_contracts.py` copies it into a
  temporary project, loads it through the real loader and checks each hook's contract (its lane completes a task and
  defers without spending an attempt, its python job runs, its check, panel, screen, projection, CLI group, commands,
  inbox kind, page, templates and static files work as documented); importing any of its modules registers nothing;
  a hook added to `ext.Registry` without a contract check fails the suite.
- **`supervisor.testing`, explicitly unstable,** for extensions' behaviour tests: `project`, `add_extensions`,
  `make_task`, an in-process `FakeLane` and `lane()`, the loop driver `drive`, `ui_client` (imported on use),
  `import_side_effects`, `reset`. Promoted from the framework's test fixtures, which now use it; no compatibility
  promise, not recorded by G2.
- **Settings offers both capacity floors.** Each engine has a 5-hour reserve (`capacity.<engine>.reserve_percent`)
  and a 7-day floor (`capacity.<engine>.weekly_gate_percent`); Settings offered only the first, so a 7-day window
  at its floor held dispatch while the floor the owner could see was far lower. Both are editable now (0-100), and
  the page labels each field by its window ("Claude: 7-day floor (% remaining)").
- **Fixed: a capacity floor changed in Settings applies without a restart.** `capacity.configure()` read
  `config/routing.yaml` once per process, so a floor the owner lowered in Settings (or by hand) reached a running
  supervisor only after a restart, and jobs kept waiting. The floors are now read again whenever routing.yaml
  changes (its modification time and size), and a Settings save reconfigures the running process directly.
- **A capacity hold says what holds it.** The deferral read "claude capacity reserve reached", naming neither the
  window nor the setting. It now names each window that holds dispatch, its remaining share, the floor and the
  routing.yaml key that sets it, and when it resets ("claude capacity held: 7-day window at 18% remaining, at or
  below its floor 20% (routing.yaml capacity.claude.weekly_gate_percent); resets 2026-10-29 19:00 UTC"). The
  Overview's "Dispatch held" tag names the floor and the setting too. A stale reading still gates, as before: a
  7-day window's remaining share only falls until it resets, so an old reading never overstates what is left.
- **Fixed: a Settings save no longer rewrites routing.yaml.** `settings.update` dumped the whole file through ruamel:
  every line ending rewritten in place (non-atomically) and long lines reflowed at 80 columns (the Studio's
  `claude.bash_allow`). It now replaces only the edited value's token, located by the YAML parser, and replaces the
  file atomically in its own line-ending style (`store.rewrite_text`); a setting the file does not have yet is
  added through a round trip that keeps long lines whole, `null` as `null` and lists indented as the file indents
  them (new internal module `supervisor/yaml_edit.py`).
- **Fixed: `supervisor migrate` no longer reformats project.yaml.** `project.write()`, which every migrate with a
  version change calls, dumped the whole file through ruamel: a long `description` reflowed at 80 columns, `null`
  written as an empty value, list items moved to the parent's indentation. It now goes through the same writer as
  Settings: a version update changes only the version's token, and a structural change (`supervisor extension new`
  appending to `extensions:`) keeps long lines, `null`, flow-style items, blank lines and indentation. Files an
  earlier migrate already reformatted stay as they are; nothing rewrites them back.
- **Both writers verify what they write.** A result is kept only when it reads back as the requested document,
  types included. An anchor edited under one of its keys, for example, cannot change the other key. A string the
  framework's YAML 1.1 reader would take as another type (`on`, `no`, `~`, `1_000`) is written quoted. When no
  rewrite reads back as the requested document, the write is refused and the file stays as it was:
  `yaml_edit.EditRefused`, which Settings reports as a settings error.

## 1.10.1 — 2026-10-25

Fixes only: a migration that rewrote current approval items on every run, a human job failing on `APPROVE`, an
interrupted migrate able to empty a file, and a racy test.

**Migration:** none added. `supervisor migrate` records the new version and, unlike 1.10.0, leaves approval items
the loop opened alone. Its dry run on a project at 1.10.0 lists only `project.yaml: framework_version 1.10.0 →
1.10.1` (plus any pre-1.0.0 response it would still map). **Rollback** to v1.10.0, in a maintenance window: drain,
stop, check out `v1.10.0`, `supervisor migrate` (records 1.10.0 again, and rewrites `APPROVE` in approval items as
1.10.0 always did), `supervisor doctor`, start; nothing 1.10.1 writes is new to 1.10.0.

- **Fixed: `supervisor migrate` no longer rewrites the approvals the framework opens.** The 1.1.1 migration
  (`migrations._approval_responses`) mapped `APPROVE` → `ACCEPT` in every inbox item's `allowed_responses`, but
  `APPROVE` is the current spelling there: the loop writes `inbox.APPROVAL_RESPONSES` (`APPROVE`, `REJECT`, `MODIFY`)
  into every approval item it opens. So every `supervisor migrate` rewrote, and re-dumped, each approval item opened
  since the last run, answered ones included. The migration now maps only the pre-1.0.0 `MORE_LIKE` and `REGENERATE`
  there; `APPROVE` → `ACCEPT` in a recorded approval response (`ApprovalResponse` has no `APPROVE`) is unchanged.
  Items an earlier run already rewrote are left as they are: their buttons read `ACCEPT`, which answers an approval
  the same way (`inbox.APPROVAL_ALIASES`), and answered items are history. No state or config shape change.
- **Fixed: a human job that offers `APPROVE` completes on it.** A `human` job may offer the approval list
  (`job.allowed_responses: [APPROVE, REJECT, MODIFY]`). Its inbox item belongs to the attempt, so the approval aliases
  never applied, and the adapter completed only on `ACCEPT` or free text: answering `APPROVE` failed the job. 1.10.0's
  migrate hid this by turning such a list into `ACCEPT`; with that rewrite gone the adapter now reads `APPROVE` as
  `ACCEPT` itself (`adapters/human.py`). Other custom words keep their meaning: anything but `ACCEPT`, `APPROVE` or
  free text still fails the job, with the answer as the note.
- **Fixed: an interrupted `supervisor migrate` can no longer empty a file.** `store.rewrite_text`, which migrations and
  `project.write` use (the `framework_version` update, `supervisor extension new`), truncated the file and then wrote
  it, so a process stopped in between left an empty file. A rerun then read it as an empty document and had nothing
  to migrate, so the content was lost (since 1.8.4). It now writes a temporary file and replaces the original
  atomically, like every store write, keeping the file's LF or CRLF; an interruption leaves the old file whole, and
  the rerun finishes the job. The rewritten file keeps what the old one had, as the in-place write did: its owner,
  group and permissions (POSIX mode bits and, on Linux, POSIX ACLs and every other extended attribute, so a
  directory's default ACL cannot add a grant the old file did not have; on Windows the DACL, including protection
  from inheritance; other POSIX systems keep owner, group and mode bits only), and its links
  (a symlink's target is rewritten and the link stays). The temporary file is private from its creation (POSIX mode
  0600; on Windows a DACL granting only its owner, set in the call that creates it), so the new content is never
  open to other accounts before the old file's access is applied, nor in a temporary file a killed process leaves
  behind. When that cannot be kept — a file owned by another account,
  a group the writer is not in, a file with several hard links, a file the account may write but not delete (the
  in-place write of 1.10.0 could rewrite that one; a replace cannot) — the file is not rewritten: `supervisor migrate`
  stops with an error naming it, the file is left exactly as it was, and a rerun after fixing it finishes the job.
  The temporary copy is removed; on Windows an old DACL that denies even its owner the right to change it (an OWNER
  RIGHTS entry) can prevent that, and the error then names the copy left behind, which has the old file's access.
  Extension authors: `atomic_write_text` (`_vendor/contract.py`) takes two optional arguments, `newline` (as
  `open()`'s) and `keep` (keep the existing file's access and links, or raise the new `CannotKeep`); both default to
  the old behaviour.

- **Tests: the diagnosis launch test no longer races its fake agent.**
  `test_a_diagnosis_launch_does_not_flip_the_blocked_task_to_in_progress` asserted that a diagnosis job was still in
  flight right after launch, but the fake manager could answer, and the same loop iteration ingest its diagnosis,
  before the assertion ran (reproducible on 1.9.0 and 1.10.0 by holding `submit()` until the fake exits). The fake
  (`tests/fixtures/fake_diagnosis.py`) now waits for a release file when `FAKE_DIAGNOSIS_HOLD` is set; the test checks
  the launch state while it is held, then releases it and waits for the job to finish. The same race, and the same
  hold, in `test_exhausted_retries_dispatch_one_manager_diagnosis_not_an_owner_item`, whose fake manager could requeue
  the task before it was checked as stopped. Test-only: no framework code changes.

## 1.10.0 — 2026-10-25

Approval evidence, one log line per owner action, progress reports from long jobs, a `claude_api` lane that reroutes
work when the prepaid credit is spent, and two status/render fixes first prepared as 1.9.1. A feature release ahead
of phase 2 of the boundary plan, so the public extension API and gate G2 move to 1.11.0 and extension packages (G5)
to 1.12.0 (`docs/BOUNDARY.md`, the framework skills). No extension hook added or changed; extensions need no change
(the `Adapter` base class gains two optional methods with defaults, below).

**Migration:** none; `supervisor migrate` only records the new version. No state or config shape change needs one: a
job record gains the optional `progress_stage`, `deferred_lane`, and `rerouted_to` / `rerouted_from` on a reroute; a
task's review record gains the optional `report_sha256`; log lines written by a command gain `command`;
`routing.yaml` gains the optional `claude_api.exhausted_recheck_hours`; `state/runtime/claude_api_credit.json`
exists only after a credit refusal; renders take the new lock file `state/.render.lock` (covered by the template's
`state/.*.lock` ignore rule).

**Rollback** to v1.9.0, in a maintenance window: (1) with 1.10.0 still checked out, drain (`supervisor drain`) until
no job is running — every iteration 1.10.0 re-queues a reroute a crash interrupted, which v1.9.0 cannot do; (2)
`supervisor doctor` must show `jobs` OK (no task's newest record is an un-requeued `REROUTED`); (3) stop, check out
`v1.9.0`, `supervisor migrate` (records `framework_version` 1.9.0 again), `supervisor doctor`, start. What 1.10.0
wrote stays readable by 1.9.0: the extra job-record and review-record fields are ignored, a closed `REROUTED` record
is a finished attempt, a successor it queued is an ordinary PREPARED record, and a record waiting on a spent lane
waits for its time. Or restore the snapshot taken before the upgrade.

- **An approval shows what it approves.** A delivery approval in the inbox used to show a one-paragraph summary and
  the artifact patterns, so the owner approved without seeing the result. The card now renders, from the task record
  at view time (so items already open show it too): every file frozen in the digest (text in place, images as
  thumbnails, each openable by index at `/tasks/<id>/files/<n>`, never by a browser-supplied path), the verification,
  and each live review's verdict, summary and findings. A declared artifact the digest does not cover is named in a
  warning, since a later change to it would not invalidate the approval. An approval whose digest is no longer the
  task's current one says so and shows no files. What is shown is what was frozen: the delivered paths must still hash
  to the digest and each file's bytes are checked against their manifest entry before they are shown, thumbnailed or
  served (`/tasks/<id>/files/<n>?digest=`); otherwise the card says the files changed and shows none. An approval's
  own `evidence` list never reads files: references outside the delivery are listed by name only. A review's summary
  and findings come from the review job's own structured result — the object its verdict was read from, frozen when
  the review is ingested (`review_report.json` in the run dir, hashed from the bytes written;
  `ReviewRecord.report_sha256`); a `supervisor task review` report is hashed when the command is submitted, and a
  report it cannot hash (missing, a link, over 4 MiB) is refused; a command carrying an explicit null hash is recorded
  without one. They are shown, and quoted to escalation's revision brief, only while that file still hashes to it
  (`tasks.review_report`, which also unwraps the `claude_api` result envelope); a changed or linked report, or a
  review recorded before 1.10.0, keeps its verdict and says the rationale is unavailable. The verification shown is
  the recorded outcome and detail. The task page's artifact list is replaced by the same view
  (`supervisor/ui/delivery.py`, `partials/delivery.html`).
- **Decision records are frozen in the delivery digest.** Every declared artifact under `state/` was dropped as a
  store file, including `state/decisions/*.md`, so a task whose acceptance is "decision recorded" was reviewed and
  approved without its decision record in the digest. `state/decisions/` is now authored state
  (`tasks.AUTHORED_STATE`, used by both the loop and `record_delivery`); the rest of `state/` is still dropped.
- **One log line per owner action.** Every command wrote a generic `command` line next to the handler's own event,
  so one **+1h** click logged twice (`job_extended` + `command`). Lines written while a command runs now carry its
  id (`tasks.command_scope`), and the generic line is written only for a failed command or a handler that logged
  nothing.
- **Progress reports from long jobs** (`supervisor/progress.py`). A job writes `<run dir>/PROGRESS.json`
  (`stage`, `detail`, optional `done`/`total`); delivery briefs for `claude_cli` and `codex_cli` ask for it before
  any step longer than a few minutes, and every subprocess job (the `process`, `claude_cli` and `codex_cli` lanes,
  and what it starts) inherits `SUPERVISOR_PROGRESS_FILE` for `progress.report()`; an in-process `python` job can
  pass its run dir's file to `report(file=...)`. The *Now running* row and the agent page show the stage and a
  bar; the loop logs `job_progress` once per stage change. Advisory only: never evidence for verification, and a
  malformed or oversized report (it is the job's file) shows nothing and never fails the job or a page.
- **Fixed: `supervisor status` is a read.** It rendered STATE.md and printed the result, so asking for the status
  rewrote a file while the loop held `state/.lock`, and since no runtime record was passed it always reported the loop
  as not running. It now prints the same lines (`render.state_lines`, with the live `state/runtime/supervisor.json`)
  and changes nothing in the project: it creates no directory and sets up no extension (setup is project code, and
  loading reconciles `state/runtime/projection_paths.json`), and extension command groups (`registry.cli`) are now
  looked up when a command name is not a built-in one, or when the whole command list is wanted (`supervisor --help`,
  top-level shell completion), however the CLI is invoked (a process, `app(...)`, a test runner) — before, every
  import of the CLI set the extensions up. Resolving a built-in command never sets an extension up; running one may (a
  writing command loads the project's extensions, `status` does not). The groups are kept per project root and rebuilt
  when that root's extension registry is reloaded. `supervisor render` still re-renders the files.
- **Fixed: a slower render can no longer overwrite a fresher one.** Every projection — the six built-in views and each
  one an extension registers (`registry.projection`) — reads and writes under `state/.render.lock`
  (`render.render_lock`), so two renderers (the loop and an owner command, say) are ordered: the one that enters
  second reads only after the first has written. It is a separate lock from `state/.lock`, which the loop holds while
  it renders, and it is reentrant within a thread, so a registered projection may call a built-in `render_*` function.
  A projection that cannot get the lock within 30 s (a wait for the lock, not a budget for the render) is skipped and
  logged (`render_error`, `projection_error`) and its previous file stays; `render_all` goes on with the next one. The
  lock orders each projection's own read and write; it does not make the set of files one snapshot.
  `render.phase_and_gates_passed()` is an internal helper (STATE.md's phase and passed gates, used by `state_lines`),
  not part of the extension API that 1.11.0 defines.
- **`claude_api`: a spent credit balance moves work instead of failing it.** The Console's "credit balance is too
  low" refusal (nothing billed) used to be an HTTP-error FAILED attempt, which the loop then retried and escalated.
  Now `submit()` raises the new `LaneUnavailable` (a `CapacityDeferred` subclass in `_vendor/contract.py`: no model
  ran, no attempt consumed) and marks the lane spent in `state/runtime/claude_api_credit.json` until
  `claude_api.exhausted_recheck_hours` (default 24, new optional key) have passed; a served request clears it.
  While spent, `credits_remaining()` is false, so `--unattended` stops switching `claude_cli` picks onto credits.
  The loop (`Supervisor.launch`) handles `LaneUnavailable` for a delivery whose own lane differs (the switch had
  moved it): the record is closed `DONE`/`REROUTED` with `rerouted_to`, the task is dispatched on that lane, and
  `repair_continuations()` finishes a reroute a crash interrupted. Anything else (a task pinned to `claude_api`, a
  review) waits PREPARED until the recheck. A reached `monthly_budget_usd` is the same gate (lifts next calendar
  month) instead of a FAILED attempt; `monthly_budget_usd: 0` still disables the lane. Such a wait ends early once
  the lane reports itself ready: the new owner command `lane.reset {"lane": "claude_api"}` (after a top-up or a
  new billing cycle) or a raised budget releases the waiting records on the next iteration. Extension authors: two
  optional `Adapter` methods back this, `lane_ready(layout)` and `reset_lane(layout)`, both defaulting to "no".
  The refusal is matched exactly (HTTP 400, JSON `error.type` `invalid_request_error`, the credit message);
  anything else stays an ordinary FAILED attempt.
  No migration: the new key is optional and the state file is absent until a refusal.

## 1.9.0 — 2026-10-25

Boundary baseline: phase 1 of the boundary plan (`docs/BOUNDARY.md`), reviewed in three rounds and accepted.

No state or config shape change; `supervisor migrate` only records the new version.

- **Gate G1, the boundary check.** `supervisor boundary check` / `python -m supervisor.boundary` (standard library
  only, so it runs from a bare checkout) over `supervisor/`: (a) no static import of a project or extension module
  and dynamic loading only in the extension loader; (b) no drive-letter or home-directory path, non-loopback host,
  e-mail address or registered user name; (c) model and vendor names only in the files `boundary.toml` lists (the
  baseline recorded by this first run: 33 files). Every exception is an explicit, commented entry in
  `boundary.toml`; registered projects' domain words are reported as supporting evidence, never as a failure.
  Exit 0 clean, 1 violations, 2 when it cannot run. `tests/test_boundary.py` holds a prohibited and a legitimate
  example for every rule. Review round (REVISE, three P2 on G1): a loader function is judged wherever it is
  referenced, however it is named (`import_module as load`, assigned, `getattr`, `functools.partial`, passed to
  `map`, `runpy`); `.local` (multicast DNS: a real machine) is no longer a reserved name, the schemas' `$id` host
  `supervisor.local` is listed explicitly; a root without the `supervisor/` package exits 2 instead of passing.
  Second round (REVISE, one P2 here): the alias analysis only grows (a name keeps every loader any binding gives
  it), so a rebinding or a cycle can no longer keep it from ending; a name that may be a path loader is judged as one.
- **CI** (`.github/workflows/ci.yml`): G1, and the suite on Linux and Windows (G3), on every push to `main` and
  every pull request; meant as required checks under branch protection on `main`.
- **Template smoke test** (`tests/test_template_smoke.py`): `supervisor init` → `supervisor doctor` → one task on the
  `process` lane through the loop to COMPLETE.
- **Pre-commit hook** `.githooks/pre-commit` runs G1; a clone opts in with `git config core.hooksPath .githooks`.
- **Skills:** `framework-change` replaces `supervisor-update` (whose rollout step, `git pull` in the framework clone,
  contradicted the tag-only rule doctor enforces since 1.8.0); `framework-release` (G4: version rule, changelog,
  gates on the release commit, review checklist, tag after ACCEPT) and `migration-author` (dry run, idempotent,
  changes only what it names, line endings kept, interruption-safe, stated rollback) arrive with it.
- **Docs:** `docs/BOUNDARY.md` (layers, rules, gates and which apply when); `UPDATING.md` gains "Pinned checkouts",
  the maintenance-window path for a project on a tagged worktree.

## 1.8.4 — 2026-10-25

Fixes from the Studio migration's phase-4 rehearsal (its record PHASE4.md, findings F2–F5), plus the licence.
No state or config shape change; `supervisor migrate` only records the new version.

- **The loop polls Codex capacity only when something can use the Codex lane** (F2). `codex_cli` is always
  registered, so every iteration asked the installed CLI for its usage windows (an app-server RPC) whatever the
  project's config said — a rehearsal copy with only fake lanes still reached it. `scheduler.lane_in_use()` now
  decides: an active role whose `adapter` or `fallback.adapter` is the lane, a `cost_lanes` override naming it, a
  live task that resolves to it, or an open job record on it. The adapters' own capacity checks before a launch are
  unchanged.
- **`supervisor doctor` names the project root** in the `project` check (F3), so a rehearsal can show which tree it
  examined.
- **`supervisor task import --dry-run`** (F4): the import's full validation (seed contracts, graph, ownership,
  artifacts, and each `--update-contracts` refresh), then what would be created and updated; writes nothing and
  logs nothing, so it is safe while a supervisor runs. `commands.seed_path()` is the one seed-path check for both.
- **Migrations change and report only what they change, and keep line endings** (F5). The 1.0.0 `local_gpu → local`
  migration reported (and rewrote) any file that mentioned `local_gpu` anywhere, so a second `migrate` reported the
  same files again; it now rewrites only a task's own `cost_category` field, found through the YAML node tree, so
  an example of the old value inside a note or a quoted string stays as written. Each field is rewritten once, in file order
  (a YAML alias shares its anchor's span), and only the value's own scalar token (from the YAML scanner),
  replaced in its quoting style: an anchor or tag before it keeps its name, and an escaped or folded spelling of the
  value is rewritten whole. Every migration write, and `project.write()`,
  goes through `store.rewrite_text()`, which writes only when the content changes and keeps the file's LF or CRLF
  style (`Path.write_text` turned LF files into CRLF on Windows).
- Tests: `test_capacity_poll.py`; the owner decision-item tests the Studio carried (REQUEUE salvage, DEFER, CANCEL on
  a review-blocked task) moved here as `test_owner_decision_items.py`.
- **Licence.** The framework is licensed under Apache License 2.0 with the Commons Clause (`LICENSE`): free to use,
  modify and share; not to be sold, nor offered as a product or service whose value derives substantially from it.
  `pyproject.toml` declares it; the README explains it. No code change.

## 1.8.3 — 2026-10-24

Review round on 1.8.2: `tools/test_windows.ps1` did not parse — `"$out:"` inside a string is read by PowerShell as a
scope qualifier. Now `${out}:`; the other `$out` interpolations are braced too. No other change.

## 1.8.2 — 2026-10-24

Review round on 1.8.1 (verdict REVISE, one remaining finding on the Windows validation script).

- **`tools/test_windows.ps1` fails loudly when its results file cannot be written.** `Set-Content`/`Out-File`
  errors are non-terminating by default, so a busy `runs/pytest_windows.txt` (another run holding it) let the
  script continue, show the previous run's output and exit 0 — a false pass. Every write to the results file is
  now a terminating error: a busy or unwritable log ends the run with exit 2 before pytest starts (or voids it if
  the file becomes unwritable mid-run); a log that ends without a pytest summary exits 3; otherwise the exit code
  is pytest's own. `tools/test_windows_selfcheck.ps1` holds the results file open, runs the script and expects
  exit 2 with the file untouched.

## 1.8.1 — 2026-10-24

Review round on 1.8.0 (verdict REVISE): two reproduced findings.

- **A failed required extension no longer unlocks the inbox items it owns.** When a required extension failed to
  import, its `inbox_kind()` registrations never ran, and the generic answer path read the missing metadata as
  permission: a `labelling` item could be closed by `inbox.answer()`, the `inbox.answer` command or the inbox form
  without the extension's per-option validation, and the inbox page rendered its blind rows. Now a kind that is
  neither built in nor claimed, while any required extension is blocking, has an owner that cannot be established
  (`inbox.ownership_unknown`): the generic path refuses (the error names the failed extension), the card is rendered
  blind and without a form and says why. Built-in kinds (`gate`, `decision`, `approval`, …) stay answerable, so the
  owner can still act; with every extension loaded, an unclaimed kind is ordinary data as before.
- **`doctor`'s framework check counts untracked files.** `updates.runtime_checkout()` ran `git status
  --untracked-files=no`, so a tagged checkout with an extra, untracked Python module was reported clean — and an
  editable install imports that module like any other. The status now includes non-ignored untracked files
  (`untracked` lists them, the check names them), and a `git status` that fails is a failed verification
  (`status_error`, `dirty: None`, FAIL "could not be verified"), never "clean".
- `tools/test_windows.ps1` sets `PYTHONPATH` to the checkout for the run (process-local, so a venv that does not
  have the framework installed can still run the updater tests, whose subprocesses import `supervisor`) and exits
  with pytest's exit code instead of 0.
- Tests: the owned item after its extension stops loading (API, command, UI render and UI answer), the unclaimed
  kind with everything loaded, and a real tagged temporary repository for the cleanliness check (untracked file,
  ignored file, failing `git status`).

## 1.8.0 — 2026-10-24

Seven extension hooks and three fixes, for a project whose work runs on one heavy local resource, reviews images in
its own Control Center pages and screens content by what a job *is*. Nothing in them is specific to any project.

- **A model-lane job can hold the resource lock** — `limits.resource_cost_categories` (routing.yaml, default `[]`).
  The single resource slot (`max_resource_jobs`) used to be held only by adapters declared `gpu = True`; an agent
  session doing the resource's own engineering work (installing into it, starting it, benchmarking it) ran on
  `claude_cli` and could overlap with a lane job on the same card. Listing a cost category (`[local]`) makes the
  *delivery* of such a task hold the slot on any lane (`gate.holds_resource`), the scheduler counts it in a plan,
  and the job record's `gpu` flag follows the same rule so a restarted loop re-adopts the lock.
- **`registry.command(kind, fn)`** — an extension's owner command kind, applied by the loop, the Control Center's
  direct path and the CLI like a built-in; a kind that already exists is an error, never a silent overwrite.
- **`registry.inbox_kind(kind, label, page=, answerable=, show_evidence=, score_keys=)`** — an inbox item kind an
  extension owns. The inbox page groups it under its label, links the extension's page, hides the per-option rows
  and their scores when `show_evidence=False` (blind labelling: the card shows a count), and drops the generic form
  when `answerable=False`; `inbox.answer()`, the `inbox.answer` command and the UI route refuse to close such an
  item (the error names the page), while the extension finalises it with `inbox.answer(..., internal=True)`.
  Before, unknown kinds fell under "Agent questions" with their scores visible and a working answer form.
- **Job-aware packet screens** — `assert_packet_clean(text, layout, where, *, job=None, task=None, lane=None)`.
  The hosted lanes (`claude_cli`, `codex_cli`, `claude_api`) pass the task's `job` block, the Task and the lane name
  from `prepare()`; a screen receives exactly the keyword arguments it declares (`**kwargs` gets all), so a screen
  can refuse on a job's channel or measured content classes, and `fn(layout, text, where)` screens keep working.
  Decided from the signature, never by catching `TypeError`.
- **`registry.template_dir(path)`** — extension Jinja templates behind the framework's own in a ChoiceLoader, so an
  extension page can `{% extends "base.html" %}` and a framework template can never be shadowed.
- **`registry.static_dir(path, prefix)`** — extension assets at `/static/<prefix>/...`, mounted ahead of the
  framework's `/static`.
- **`supervisor.gpu`** — the shared nvidia-smi probe (`free_mb`, `used_mb`, `total_mb`, `available`): lanes,
  doctor checks, overview panels and tools outside the framework import it from one place; it never raises.
- Fix: a `required: false` extension that fails to load is logged once per process (`extension_error`) and said on
  stderr; before, it showed only in `supervisor doctor`, indistinguishable from an extension left out on purpose.
- Fix: `supervisor --help` no longer silently drops a broken extension's command group — the CLI says on stderr
  which extension did not load and that its commands are missing.
- Fix: `supervisor doctor` has a `framework` check: the installed version plus, for an editable install of a git
  checkout, its commit and tag, FAILING when the checkout is dirty or not exactly at a tag — a project must run from
  code a review accepted and a CHANGELOG names, never a working tree being edited. An installed package (no
  checkout) reports its version only. `updates.runtime_checkout()` is the underlying probe.
- Docs: `docs/FRAMEWORK_VS_EXTENSION.md` lists the new hooks; the routing template documents
  `limits.resource_cost_categories`.
- Tests: `tests/test_hooks_180.py` (hooks 1–7 and the three fixes, 17 tests).

## 1.7.3 — 2026-10-24

Migration plan phase 1b (periphery modules of the drift ledger): one port.

- **Control Center timestamps in local time.** Store values are ISO-8601 UTC; every timestamp the Control Center
  shows (activity table, log page, inbox answers, task history, notes, approvals, attempts, the heartbeat tooltip
  and the live log stream) now renders as the machine's local `YYYY-MM-DD HH:MM` through the `localtime` Jinja
  filter (`supervisor/ui/timefmt.local_stamp`; a value that does not parse is shown unchanged). Before, pages
  showed raw UTC ISO strings or a clipped slice of one.

## 1.7.2 — 2026-10-24

Fixes ported from the project the framework was extracted from (migration plan phase 1a: the drift ledger's
"upstream" rows). Generic hardening the project had made after the extraction and the framework lacked.

- **`claude_api` results were read as empty.** `collect()` returned the result-file envelope as `Result.raw`, while
  `review.ingest` reads the verdict, `loop.ingest` the declared `tests` and a diagnosis its decision off `raw`
  — so every API-lane review came back "no verdict" and every API-lane delivery lost its test evidence. `raw` is
  now the structured object itself (the envelope only when there is none, so the error still travels).
- **One paid POST per attempt, ever.** The response body is persisted (`response.json`) before the ledger append
  and the result write; a submit re-entered after a kill in that window finishes from the saved body instead
  of calling the API again, and the ledger entry is keyed by the response id so it is never billed twice
  (`claude_api.entries`, `recorded`, `record_usage(..., response_id=)`).
- **The unattended switch only moves work the tool-less lane can do.** `claude_api.unattended_eligible`: the pick
  must be the cost-category default (an explicit `job.adapter` is never overridden), the contract must declare no
  artifacts, tests or executable checks (there is no Read/Write/Bash through the Messages API), and the task or
  its role must be opted in (`job.api_credits: true` or routing.yaml `claude_api.eligible_roles`). Before, any
  `claude_cli` pick was switched once credits were available, engineering tasks included.
- **Fail-closed credential scanning** (`policy`). *There is no credential store* (absent `secrets/`, the normal
  state of a fresh clone) is now distinct from *the store could not be read*: an unreadable directory or file,
  a non-regular entry, a file too large to scan, one that is not UTF-8, a half-written `.json` token file or a
  relocated `secrets.dir` that does not exist is a scan failure (`secret_scan_problems()`), never cached, and
  while one stands `assert_packet_clean()` refuses every dispatch and the log redactor withholds every field
  value long enough to be a credential (`policy_unscreened` names why). Both halves of the packet judgement
  come from one scan. A config file that cannot be stat-ed is reported and never served from cache.
- **Numeric credentials and credential keys.** A digit-only password stored as a JSON number is collected (in
  both the file's literal and the canonical `str()` form) and screened wherever it appears — a packet, a log
  value, a log key, a numeric field name — while unrelated numbers keep their value and their type. Mapping
  keys are redacted like values; two keys that redact alike both keep their record (`[REDACTED]#2`).
- **The log's `actor` and `event` are screened.** `tasks.log()` passes both through `screen_log_identity()`:
  screened for literals and patterns, never blanked (the trail stays readable while the store is unreadable).
  `actor` is data — `commands.apply_all()` reads it from a queued command file.
- **Review verdicts are not lost over a category spelling.** `_vendor/contract` maps the near-misses reviewers
  write ("acceptance", "requirements", "bug", …) onto the canonical categories and names the offending finding
  in its error; `packets.review_brief` tells the reviewer the vocabulary; `claude_cli.follow_up(error)` quotes
  the contract's own error back to the model so it fixes only that; and `review.salvage()` ingests a finished
  review attempt whose answer parses now, before paying for a fresh review (`review_salvaged` event, `salvaged`
  on the job record).
- `tasks.record_delivery` filters the delivery's paths through `verify.excluded_files`, so an extension
  projection is never frozen as a review input (the loop already dropped it from the artifact list).
- Tests: `test_policy_hardening.py` (40), `test_review_salvage.py`, `test_delivery_paths.py`, the `claude_api`
  resume/ledger/eligibility cases, the follow-up prompt and the review-brief vocabulary.

## 1.7.1 — 2026-10-24

Review round on 1.7.0 (verdict REVISE): three time-limit edges around upgraded and interrupted jobs.

- **The first activity sample no longer kills a busy job.** `timeouts.last_active` dated a job's first record back
  to its launch, so a session adopted after the upgrade (no `activity.json` yet) that had already run longer than
  the idle limit was stopped as idle on its very first look, while its output was still growing — and so was any
  job whose first collection came late. The first sample now opens the observation window at that moment; idleness
  is judged only from two samples. A quiet job is still stopped one full idle window later.
- **One authoritative deadline.** `timeouts.effective_limits` returns what the adapter enforces (for a job prepared
  before 1.7.0, `claude_cli.model_limits` moves it onto the current policy). `agent.extend`, `status.json`, the
  time-limit handoff withdrawal, the *Now running* row and the agent page all use it; before, Extend added its hour
  to the obsolete one-hour deadline still in the handle, so the effective cap moved but the recorded one stayed in
  the past and the session was still told to stop and hand off. Extend now writes the effective deadline + 1 h to
  the handle, so the recorded and effective caps agree from then on.
- **Replaying Extend repairs an interrupted update.** The job record is written first, then `status.json` and the
  handoff request. A crash between those writes made the replay skip the rest (*already applied*): the hour was
  in the job, not in the `status.json` that `recover()` re-adopts from, and the stop request survived. A replay
  now re-applies both from the job record without adding the hour again, reports `repaired`, and logs
  `job_extend_repaired`.
- Tests: a busy legacy session through the real `ProcessAdapter` subprocess path with the real `model_limits`
  survives its first collect, a quiet one is still stopped after a full window; Extend on a session 5h55m into a
  pre-1.7.0 run; fault injection between the job write and the status write, then replay.

## 1.7.0 — 2026-10-24

Agent sessions are no longer killed at one hour while they are still working.

- **Activity-based time limits** (`supervisor/timeouts.py`). A `claude_cli`/`codex_cli` job used to get one fixed
  deadline, 3600 s after launch, and long sessions were killed mid-work. Now a job is stopped only when it is
  **idle** — its `stdout.jsonl`/`stderr.txt` have not grown and its process tree has used less than `idle_cpu_s`
  CPU seconds for `idle_s` (default 20 min; Claude Code's 30 s tool heartbeats and any child process doing work count
  as activity) — or when it reaches its **hard cap** (default 6 h). Measured per run dir in `activity.json`, so a
  restarted loop keeps measuring where the last one stopped.
- **Configurable** in `routing.yaml` `timeouts` (`idle_s`, `idle_cpu_s`, `hard_cap_s`, `by_cost.<cost_category>`),
  per role (`roles.<role>.timeouts`) and per task (`job.timeout_s` = hard cap, `job.idle_timeout_s` = idle limit,
  0 = none). Plain `process` jobs keep `job.timeout_s` (default 3600) and get an idle limit only when they ask.
- **Extend.** `agent.extend` (Control Center **+1h** on a *Now running* row, **Extend +1h** on the agent page)
  raises one running job's hard cap by an hour; idempotent per command id, written to the job record and
  `status.json` (so a re-adopted child keeps it), and it withdraws a pending time-limit handoff request. The row
  shows elapsed time against the cap and, after two quiet minutes, how long the job has been quiet.
- **A timeout is a continuation, not a failure.** A model-lane job stopped on time is requeued like a capacity
  limit (`Supervisor.requeue`, reason *time limit (idle)* / *time limit (hard cap)*): a fresh session picks up the
  `HANDOFF.md`, no revision is spent, the usual continuation cap (3, then an owner decision) applies. Ten minutes
  before the cap the session gets `HANDOFF_REQUEST.json` with reason `time limit`; a handoff written in answer is
  requeued as *time limit handoff*.
- Jobs already running when the loop is updated carry no idle limit in their spec: model lanes move them onto the
  current policy (`claude_cli.model_limits`) instead of killing them at the old hour; a deliberate cap (a chat
  turn's 600 s) is kept.
- No migration. Add a `timeouts:` block to `config/routing.yaml` only to change the defaults (the template shows it).

## 1.6.1 — 2026-10-23

Review round on 1.6.0 (verdict REVISE): owner intent wins over the stall sweep, the live refresh keeps the page
structure, and the suite can no longer start a real model session.

- **Stall sweep respects deliberate holds.** A stop the owner made (Control Center **Hold**, `set-status` by an
  `owner*` actor), a **snoozed** decision item, and a task whose latest delivery attempt is on the `human` lane (the
  owner's REJECT/MORE_LIKE) are never retried or diagnosed. The human case is linked through the attempt, not by
  comparing timestamps: the loop collects an answer after it was given, so the answer always predates the stop.
- **Live refresh no longer nests `<main>`/`<nav>`.** `live.js` handed idiomorph the parsed wrapper element; even with
  `morphStyle: "innerHTML"` that element *is* the new content, so every refresh inserted a second `<main>` (and
  `<nav>`) inside the first and shifted the page. The child nodes are now passed as an array. Checked in Chromium
  against a scratch project: before the fix 2 `<main>`/2 `<nav>` after one refresh; after it 1/1 through an automatic
  refresh, a held-then-applied refresh (draft kept in its own form) and three more in a row, with no layout shift.
- **`task.note {guidance: true}` is one idempotent write.** Note and guidance go in a single task update
  (`tasks.add_note(guidance=True)`), each keyed by the command id; a replay of a command whose note exists but whose
  guidance is missing completes the guidance (`{"repaired": "guidance"}`) instead of answering "already applied".
- **No real model client in the test suite.** An autouse guard (`tests/conftest.py`) refuses any launch of the
  installed `claude`/`codex` CLI (version/help probes allowed) and fails the test with the command it tried;
  `@pytest.mark.model_integration` opts out (`capacity_integration` does not). The tests that reached a real client
  now use fake lanes: the two loop-command tests run a python worker, and the Codex failure tests stub the diagnosis
  dispatch.
- `loop.py`: removed the duplicated `_epoch`/`scheduler_routing` helpers (`ruff check --select F supervisor` clean).

## 1.6.0 — 2026-10-23

Nothing stops silently: every failure is retried or escalated; the owner hears about it only when an action is theirs.

- A worker that ended **BLOCKED**, FAILED or PARTIAL (or crashed, timed out, was lost) used to leave its task FAILED
  with no retry, no diagnosis and no owner item — only "the worker claimed COMPLETE and verification disagreed"
  reached escalation. Now: BLOCKED → straight to the manager diagnosis (`escalation.record_failure(retry=False)`;
  re-sending the same brief reproduces the same block); a model-lane FAILED/PARTIAL → the normal revision budget,
  then diagnosis; an owner REJECT/MORE_LIKE on a `human` task stands. Issues are titled by cause (*worker reported
  BLOCKED*, *worker run failed repeatedly*, *stopped with nothing routing it*).
- The failure detail carries the worker's notes and the head of its `HANDOFF.md` (`loop.worker_report`), so revision
  briefs, diagnosis briefs, issues and owner items say *why*, not just `status BLOCKED; missing:`.
- **Stall sweep** (`Supervisor.sweep_stalled`, every iteration): a FAILED/BLOCKED task with nothing in flight and no
  open owner item, past `routing.yaml escalation.stall_grace_s` (default 600 s), is escalated once per stop (FAILED →
  retry within budget, BLOCKED → diagnosis); still unrouted a grace period later → one owner decision item.
  Deterministic lanes (process/python/extensions) are retried this way, so the grace period is their back-off.
  Owner answers and diagnosis `defer` after the stop are respected as deliberate holds.
- **Idle alert** (`Supervisor.alert_if_idle`): nothing running, nothing eligible, nothing waiting on the owner, work
  remaining, for twice the grace period → one *Pipeline idle — nothing can run* owner item listing the stuck tasks
  and their last note; once per stuck set (`state/runtime/idle_alert.json`).
- Agent sessions can be given folders outside the repository: role `add_dirs` and
  `claude.add_dirs_by_cost.<cost_category>` in `routing.yaml` (Claude Code `--add-dir`; relative entries resolve
  against the project root). Without them the CLI denies even `ls` there and the worker can only end BLOCKED.
- `task.note` with `"guidance": true` also hands the text to the next delivery brief.
- Escalation decisions carry `at` (used by the sweep). No migration; existing escalation state files are read as
  before.

## 1.5.0 — 2026-10-23

Live Control Center: open pages keep themselves current; no more F5.

- Every page polls `GET /live/version` every 2 s (only while its tab is visible). The value is a stat-only
  fingerprint of `state/` (minus applied commands, lock/temp/log files and the update-check cache), `config/` and
  `project.yaml`, cached for 1 s so many tabs cost one scan (`supervisor/ui/live.py`). `state/runtime/supervisor.json`
  counts by content without its heartbeat fields: a loop tick is not a change, a status or decision change is.
- When it moves, `static/live.js` re-fetches the page and morphs `<main>` and the sidebar (inbox badge) into place
  with the vendored idiomorph 0.7.4 (0BSD, `static/idiomorph.min.js`). Scroll position, focus and opened
  `<details>` survive. Pages also refresh every 30 s regardless, so ages and timers stay current.
- Nothing the owner is doing is lost: while a form field has been changed, a file is chosen or text is selected,
  the update is held back and the new **Live** badge in the top bar says **Updates waiting — apply**; clicking it
  applies the update and keeps typed values in their own form (matched by form action and field name). Elements
  marked `data-live-keep` (the Overview's action result) and SSE streams (log and transcript tails) are never
  touched. Inbox items, owner tasks and roadmap gates carry stable ids so a morph never pairs one item's form with
  another's.
- Clicking **Live** pauses updates in that browser (remembered); clicking again resumes. **Reconnecting…** shows
  while the Control Center does not answer; when it comes back as a new process (an update, a restart) or its
  stylesheet changed, open pages reload fully onto the new templates.
- A page that keeps its own client-side state opts out with any element carrying `data-live-off`: it is never
  refreshed and the badge reads **Live off on this page**.
- Each page is stamped (`<meta name="live-version">`) with the fingerprint taken in middleware before the route reads
  any state, so a change landing mid-render refreshes the page once more instead of being missed.

**Upgrade note.** No migration. Extension pages get the same behaviour; an extension page that renders from files
outside `state/` and `config/` refreshes on the 30 s fallback (or when project state changes). Mark any element an
extension fills in client-side with `data-live-keep`.

## 1.4.1 — 2026-10-23

Fixes from the independent review of 1.4.0.

- Fix: the owner's REQUEUE text never reached the next worker. It was saved only as a task note, which no packet
  reads, so the item was applied and the task went READY while the agent got none of the correction. It is now
  written to the task's `guidance` (as the diagnosis requeue already was), and the delivery brief carries it under
  "Guidance". Adapters also append it to a delivery prompt that did not come from the default brief (a job's own
  `prompt`/`prompt_file`, the codex fallback prompt). A review resume scopes the text to the review of that digest
  ("Owner guidance for this review"); it never reaches a delivery brief. Re-applying an item never adds it twice.
- Fix: REQUEUE did not reset an exhausted escalation budget. The spent revisions and failure signatures survived, so
  the next failure escalated at once. Every owner REQUEUE now resets them (`escalation.reset_retries`, keyed by the
  inbox item so a replay after a crash never grants a second reset).
- Fix: valid artifact paths were refused. Spaces and placeholder-like spellings (`Final Report.md`, `out/xx.json`,
  `round_N_results.txt`) are legal file names that verify fine, but `task create` and `task import` rejected them.
  `tasks.artifact_problems()` now splits **errors** (no file can ever match: absolute/escaping paths, directories,
  `<>"|` and control characters, padded or empty entries) from **warnings** (prose- or placeholder-like but legal;
  none when the entry already matches a file). Warnings are returned as `warnings`, logged, and printed by the CLI.
- Fix: projection exclusions outlived their registration. Declared paths accumulated, so changing `path=` from
  WEEKLY.md to REPORT.md kept both excluded; a projection removed from a still-configured extension stayed excluded;
  and with no projection registered, 1.2.1 legacy entries were never dropped. `extensions.load()` now reconciles the
  record with what each successfully loaded extension registers: unregistered keys go, declared paths are replaced
  (a rendered path learned from the old declaration goes with it), and legacy entries go when nothing is registered.
  Entries of an extension that failed to load, or that is not configured, are kept (re-adding one restores them).
- Fix: distinct projections with the same `__name__` (two lambdas, two closures from one factory) shared one record
  key, so rendering the second retired the first. Keys are now unique (`<lambda>`, `<lambda>#2`, ...);
  `registry.projection(fn, path=None, name=None)` takes an explicit `name` for an identity that survives reordering
  (a duplicate `name` within one extension is a setup error).
- Tests: the Windows recovery adoption test holds its child on a release flag instead of a 1.5 s sleep (a child that
  finished during the process scan was correctly adopted as finished, and the test failed). The suite no longer calls
  the workstation's real Codex usage RPC: a nearly spent real window gated the fake jobs of `test_codex_cli`. An
  autouse fixture reports the RPC unavailable; `@pytest.mark.capacity_integration` opts a test back in.

**Upgrade note.** No migration. Existing `guidance` entries (diagnosis) are read as before. A seed entry 1.4.0
refused for its spelling alone now imports with a warning.

## 1.4.0 — 2026-10-23

Owner decisions that actually reach the owner and take effect; inbox text that is never cut short; contract fixes
that reach a running loop.

- Inbox items keep their full text. `inbox.fit()` splits long prose into a `summary` (≤ 600 chars, cut on a word
  boundary) and a `detail` (≤ 8000 chars); `create(detail=...)` stores it, the Control Center shows it behind
  **Show full message** and `HUMAN_INBOX.md` renders the full text. Before, every writer hard-cut at 600 characters
  and whatever came last was lost — for an escalation that was the question and the options. Diagnosis owner route,
  delivery approvals and the human adapter all use it; a diagnosis summary is kept up to 4000 characters (was 1500).
- New `decision` inbox kind (`REQUEUE / CANCEL / DEFER`, "Stopped tasks — your decision" in the inbox). The loop
  opens one per block for a task BLOCKED with "owner decision needed" (review budget spent, continuations
  exhausted, review invalidated) — before, such a task only had an issue and a note and nothing reached the owner.
  The diagnosis owner route opens one for the FAILED task, with the question and the options first.
  `inbox.apply_decision` acts on the answer: REQUEUE resets the budget that stopped the task and resumes it
  (`resume: review | delivery | task`), CANCEL cancels, DEFER annotates; the outcome is recorded as `applied`, and
  the loop re-applies an answered item whose effect never landed.
- Fix: REQUEUE/CANCEL on a diagnosis escalation did nothing — it was a `question` item, so the answer was recorded
  and ignored. Such items written by earlier versions are recognised (from `escalation`, REQUEUE/CANCEL/DEFER) and
  acted on when answered.
- Fix: text typed next to a button ("REQUEUE use the cached outputs", the Control Center sends both as one
  response) was dropped; it is now added to the task as owner guidance.
- `supervisor task import [--update-contracts ...]` goes through the command queue (`task.import`, payload
  `{seed?, update_contracts?, create?}`), so it works while a supervisor runs. Before, it refused while
  `state/.lock` was held — and since every agent works under the loop, no agent could ever apply a contract fix it
  had made in the seed. The seed must be a file inside the project.
- Task contracts: artifact entries the floor can never satisfy are refused by `task import` and `task create`
  (`tasks.artifact_errors`): prose ("contact sheets"), placeholders (`<id>`, `{x}`, `S1..S6`, `EXP-1xx`, `round_N`),
  directories (`src/`, or an existing directory named without a glob) and absolute or escaping paths. Such an entry
  failed every delivery with "missing: …" forever. A non-glob entry that names a directory now says so in the
  verification detail instead of plain "missing". `docs/TASK_CONTRACT.md` documents the rule; its seed example
  (`src/`) is now `src/**/*.py`.

**Upgrade note.** No migration. A project whose seed still contains such an entry gets a clear error from
`task import` naming each one — fix the seed (glob or file path) and import again. Stored contracts are not
re-checked; run `supervisor task import --update-contracts <ids>` after fixing the seed to refresh live tasks.

## 1.3.0 — 2026-10-23

Control Center Overview: clearer controls and a live view of current work.

- Control row: no button is highlighted any more. The purple "suggested next action" (Pause while running) read as
  the current state. Each button is now enabled only when it applies (Start only while offline, Pause only while
  running, Resume only while paused or draining, Stop/Reload only while live) and a disabled one says why on hover.
  A decision the loop has not applied yet already counts, so Pause greys out the moment it is clicked.
- Supervisor card: the "Current task" cell is gone, so the uptime no longer sits beside a task and reads as the
  task's age. The card shows Workspace, Process (PID), **Supervisor uptime** (with "Running since HH:MM") and Open
  jobs; draining, a pending power-off and pause show as a note under the details.
- New **Now running** panel under the metric cards: every open agent/process job (owner jobs excepted; they are
  counted as "waiting for you") with task title and ID, what it is doing (Working / Reviewing / Diagnosing a
  failure / Answering chat / Verifying), role, model and effort, attempt number, running time against the job
  timeout, and a link to the live transcript; deferred jobs show when they retry. An **Up next** column lists the
  next eligible tasks.
- The panel and the Supervisor card refresh themselves every 5 s (`GET /overview/live`, card swapped out-of-band;
  polling pauses while the tab is hidden) and right after any control is clicked. Action results are kept.
- Fix: a view that cannot render no longer fails the loop iteration. A YAML record caught half-written (an agent
  editing it) made `render_all` raise on every pass; five in ten minutes stopped the supervisor BLOCKED with the
  agent's delivery still uncollected. Each built-in view is now rendered on its own: a failure skips that view
  (its previous file stays), logs `render_error` once per distinct message and `render_recovered` when it renders
  again. Extension projections already behaved this way.

## 1.2.3 — 2026-10-23

Fourth review round (on 1.2.2, commit 52c639e).

- Fix: chat replies stalled on a freshly stamped project — 1.2.2 made the gate read the `conversation` lane's
  `active` flag and the template ships it as false. The chat lane is owner-driven, not a role that has or lacks
  work, so `active` is ignored for it (projects stamped by 1.2.2 or earlier need no change; the template now says
  `active: true` with a note that it is informational).
- Fix: projection exclusions are recorded per projection (`state/runtime/projection_paths.json` `projections`:
  `<extension>:<function>` → declared paths + last rendered path). A projection that starts writing REPORT.md
  instead of WEEKLY.md retires the old file (1.2.2 kept both excluded forever). A 1.2.1 flat `paths` list and the
  1.2.2 `by_extension` map are read as unattributed entries and honoured until every projection has rendered once
  on the new code, so an upgrade never rejects an untouched view before the first render.

## 1.2.2 — 2026-10-23

Third review round (on 1.2.1, commit c7274b9).

- Fix: the launch gate receives the role of every agent job kind — a diagnosis runs as its `diag_role`, a chat
  turn as the `conversation` lane role, a review as its reviewer — so `active: false` holds diagnoses too (they
  used to go SUBMITTED).
- Fix (process adapter): `recover()` looks for `result.json` again after the process scan. A child that finished
  while the scan ran was reported lost and launched a second time (duplicate execution of one attempt).
- Fix: recorded projection paths are keyed by the extension that rendered them (`state/runtime/projection_paths.json`
  `by_extension`); removing the extension from project.yaml removes its exclusions, so a hand edit to a former
  projection file is an undeclared write again.

## 1.2.1 — 2026-10-23

Second review round (on 1.2.0, commit 52cf048).

- Fix: a role configured `active: false` is refused by the launch gate itself (`gate.check`), not only by the
  scheduler's plan — a job queued before the role was disabled, a review for a dormant reviewer role (skipped in
  `review.pending`, logged once as `review_skip`) and a direct dispatch all stay PREPARED/waiting instead of going
  RUNNING/SUBMITTED.
- Fix (updater): a doctor child that exits non-zero without printing a FAIL line (an exception on the new code),
  and a pip reinstall that fails with empty output, fail the update; the exit code is the verdict, FAIL lines are
  detail.
- Fix: a `project.yaml` that cannot be parsed blocks the loop and the packet screens like a failed required
  extension (its `extensions:` list — and the policy screens it names — cannot be read), instead of loading no
  extensions at all.
- Control Center: the Roadmap has a **Pass gate** action (with the evidence an evidence gate requires); the
  Control Center acts as owner, so an owner gate is passed and acknowledged in one step and needs no CLI.
- Fix: projection files learned at render time are recorded in `state/runtime/projection_paths.json`, so the
  verify child and a restarted loop exclude them too (an untouched WEEKLY.md came back as an undeclared write
  after a restart). Declaring `registry.projection(fn, path=...)` remains the explicit form.
- Tests: scratch projects under `runs/` are fenced from the framework clone's git (`GIT_CEILING_DIRECTORIES`), so
  the suite passes with its default in-repo basetemp.

## 1.2.0 — 2026-10-23

Review round on 1.1.0 (seven blocking, eleven should-fix findings). No state migration is needed for projects
created with 1.0.0 or later; the one migration below only touches approvals recorded before 1.0.0.

- Fix (Control Center): Stop/Pause/Resume and every other HTMX action returned HTTP 500 on current Starlette
  (`TemplateResponse(name, context)` was removed upstream) although the command had been applied. The action
  helper now renders request-first like every page. The test suite exercises real HTMX requests and never fetches
  `origin` (`SUPERVISOR_UPDATE_CHECK=0`, also honoured by `supervisor ui` on offline machines).
- Fix (scheduler): `roles.<role>.adapter` in routing.yaml now decides the provider of agent work (model cost
  categories); it used to be ignored in favour of `cost_lanes`, so an engineer configured for Codex still ran on
  Claude. A role with `active: false` is never dispatched: its tasks are parked with a `schedule_skip` log entry.
- Fix (scheduler): the unattended `claude_cli → claude_api` switch is opt-in through
  `capacity.unattended_prefers_api_credits: true`; it used to spend API credits whenever a key was present.
- Fix (extensions): a configured extension that fails to import now blocks the loop (`BLOCKED`, reason names the
  extension) and makes `assert_packet_clean` refuse every packet, instead of silently running without the hooks
  it provides (a failed privacy screen let customer text through). `extensions: [{name: x, required: false}]`
  opts an extension out of blocking. Extensions load by file path under a per-project namespace
  (`supervisor_ext.<hash>.<name>`), no `sys.path` mutation: two projects' same-named extensions no longer share an
  implementation and an extension called `json` no longer resolves to the standard library.
- Fix (loop): an adapter that raises in `prepare/recover/submit/collect` fails that attempt (error on the job and
  the task) instead of consuming the loop's error budget and blocking the whole project.
- Fix (verify): extension-registered projections (`registry.projection(fn, path=...)`) are excluded from
  "undeclared writes" like the built-in views; a projection re-rendered during an attempt no longer fails it.
- Fix (gates): gates declare `owner_ack_required` / `evidence_required` (the template's G0 sets both). A gate is
  open only when PASSED and those are satisfied; an agent actor cannot pass an owner gate and PASSED without
  evidence is refused. Absent keys keep the old behaviour. `supervisor gate set` takes `--actor owner`.
- Fix (store): atomic writes use `tempfile.mkstemp` temp names; PID + `time_ns()` collided between threads on
  Windows (PermissionError on replace). The concurrency test fails on worker exceptions.
- Doctor: new `policy` check (`policy.config_problems()`), as the policy runbook already claimed.
- Docs/template: agents never commit (the supervisor checkpoints); the setup skill interviews and writes the plan
  before `supervisor init` and says which steps are CLI and which are file authoring; architecture/runbook
  statements corrected (owner-forced COMPLETE, `.jsonl` conversations, which commands queue vs refuse while the
  loop runs, what drain does); the Control Center launcher sets project-local TMP/TEMP; remaining references to
  the framework's source project removed and the blacklist test now covers `_vendor` and the template.

- Fix (updater): the updater stops a running loop (`loop.stop`, waits for `state/.lock` with a bounded timeout) before
  pulling and migrating — `supervisor migrate` refuses while the loop holds the lock, and the update used to report
  success anyway. A failing reinstall, migration or newly failing doctor check now fails the update (no restart,
  the error says what to do); a stopped loop is started again on the new code.
- `supervisor.launcher`: restart-on-exit-3 wrapper for the loop; the Control Center's Start button launches
  through it, so a UI-started loop survives Reload and framework updates like the console launchers.
- **Migration**: owner approval responses recorded before 1.0.0 are mapped to `ApprovalResponse`:
  `MORE_LIKE` → `REDO`, `REGENERATE` → `REDO` (both asked for another attempt at the same deliverable),
  `APPROVE` → `ACCEPT` (the inbox spelling). A task holding an old value could not be loaded. Inbox items'
  `allowed_responses` are mapped the same way. `supervisor migrate` applies it.
- Doctor: a malformed `project.yaml` or one without `framework_version` fails the project check (it used to pass
  on defaults); `supervisor migrate` refuses without a usable `project.yaml` instead of reporting a version change.
- `project.write` (migrations, `supervisor extension new`) preserves the comments and layout of `project.yaml`
  (ruamel round-trip, as Settings does for routing.yaml) instead of rewriting it from scratch.

## 1.1.0 — 2026-10-23

- Framework update check (`supervisor/updates.py`): at launch, `supervisor ui` fetches `origin`, lists the commits
  the clone is behind (with the version change and a migration hint from the changelog) and, on a terminal, asks
  whether to apply; `supervisor run` only reports. The Control Center shows the same as a banner with an **Update
  now** button (a background thread refreshes the check every 30 min). Applying = `git pull --ff-only`, reinstall
  the package, `supervisor migrate` + `supervisor doctor` in a child on the new code, `loop.reload` if a loop is
  live, and the UI exits with code 3 so the launcher restarts it. New CLI: `supervisor update [--check] [--yes]`.
  A dirty framework clone blocks the update and says so.
- Template: `tools/run_control_center.ps1` (restart-on-exit-3 wrapper); `start_control_center.bat` uses it.
- Guard rails: `supervisor init` refuses a target inside the framework clone; the framework `.gitignore` ignores
  project-shaped paths at its root so project work cannot be committed to the framework repo by accident.
- Fix: the Overview's Disk bar filled with the free fraction while its caption said "used"; it now fills with the
  used fraction (extension panels pass their own `progress` and are unaffected).
- Fix: `tasks.import_seed` now writes `status: NOT_STARTED` and the runtime fields explicitly, so a hand-written
  seed dispatches on the first run (the first launch used to crash on a missing `status` key).

## 1.0.0 — 2026-10-22

Initial release, extracted and generalised from a project-specific supervisor.

- `supervisor` package: loop, task store with the single `advance()` rule, one launch gate (decision, capacity,
  limits, file ownership), deterministic verification with a tracked verify job, independent reviews on frozen
  inputs with same-vendor fallback, retry budgets and the manager → architect → owner diagnosis route, owner inbox
  with digest-bound approvals, owner tasks, decisions, issues, chat threads, accepted-work git checkpoints, drain
  and shut-down-when-idle, credential policy (packet refusal, log redaction, child-env scrubbing).
- Adapters: `python`, `process`, `claude_cli`, `claude_api`, `codex_cli`, `human`.
- Control Center (FastAPI/HTMX): overview, roadmap, tasks, agents (live transcript, terminate, note), inbox
  (answer/snooze/reopen/gate ack/upload), chat with convert-to-task/decision/issue/owner-task, log, owner tasks,
  settings.
- Project boundary: `project.yaml` discovery (`SUPERVISOR_ROOT` or nearest ancestor), `supervisor init` from a
  packaged template, `extensions/` with a registry (adapters, doctor checks, overview panels, UI routers, packet
  screens, projections, CLI groups, python jobs), `supervisor doctor`, `supervisor migrate`, version check.
- Generic template: five role cards (senior_pm, chief_architect, engineer, code_review, research_strategist),
  four result schemas with samples, routing/policy/gates/seed templates, Windows launchers and a POSIX setup script.
- Setup skill (`.claude/skills/supervisor-setup`) wrapping `docs/SETUP_INTERVIEW.md`; `supervisor-update` skill.

**Migration** (from the pre-1.0 project-specific supervisor, if you ever point it at such a state directory):
`limits.max_gpu_jobs` → `limits.max_resource_jobs`; `cost_category: local_gpu` → `local`; inbox responses
`MORE_LIKE`/`REGENERATE` → `REDO`. `supervisor migrate` handles the first two.
