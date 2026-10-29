# The plugin standard

One shape for every plugin, so any project can install one and any contributor can read one. A project-local
plugin follows the same rules as a packaged one, minus the packaging; promoting it is then a move, not a rewrite.
`PLUGIN_API.md` is what a plugin may call; this document is what a plugin must declare. Both are part of
the boundary (`BOUNDARY.md`); the standard arrives with 1.12.0 (boundary plan phase 3).

Three names are kept apart:
- **The identity**: the stable name in the manifest, which saved state refers to. Only this matters to the framework.
- **The Python package or module name.**
- **The distribution name.**

For example, the Textgen lane's identity is `text_model`, because tasks and job records already name that adapter. Its
package could be `supervisor_plugin_textgen` and its distribution `supervisor-plugin-textgen`.

## The manifest: `supervisor-plugin.toml`

Read by the framework before anything the plugin ships is imported (`supervisor/manifest.py`). The schema and
migrations are module names here, imported only once the plugin is approved.

```toml
identity = "text_model"                       # required; lower case letters, digits and _, starting with a letter
version = "1.0.0"                            # required; the plugin's own version (PEP 440)
framework = ">=2.0,<3"                       # required; the framework versions it supports (a PEP 440 range)
migrations = "supervisor_plugin_textgen.migrations"   # optional; a module exposing MIGRATIONS

[provides]                                   # what setup() registers; nothing else may be registered
adapters = ["text_model"]
doctor_checks = ["text_model_server", "textgen"]
hooks = ["overview_panel"]                   # hooks that register something without an identifier of its own

[legacy_ids]                                 # identifiers older than the namespace rule, kept as they are
doctor_checks = ["textgen"]

[config]                                     # optional; the plugin's own configuration section
section = "plugins.text_model"             # always plugins.<identity>
schema = "supervisor_plugin_textgen.config:Settings"   # module:Class, a pydantic model of that section
legacy_sections = ["text_model"]              # optional; the top-level sections its values came from before 1.12.0

[requires]                                   # optional
resources = ["gpu"]                          # shared resource policy it reads (resources.gpu)
services = ["http://127.0.0.1:11434"]        # what it talks to, shown by doctor
plugins = ["render"]                       # identities it calls into; they load first
```

Parsing is strict: an unknown key, an unknown kind or a malformed value is a problem, never ignored. Every problem is
reported at once, so a manifest is fixed in one pass.

### `provides`

| Kind | Identifiers it lists | Registered with |
| --- | --- | --- |
| `adapters` | lane names | `registry.adapter()` |
| `python_jobs` | job names | `registry.python_job()` |
| `commands` | command kinds | `registry.command()` |
| `inbox_kinds` | inbox item kinds | `registry.inbox_kind()` |
| `doctor_checks` | check names (`Check.name`) | `registry.doctor_check()` |
| `cli` | command group names | `registry.cli()` |
| `routes` | Control Center path prefixes, each starting with `/` | `registry.ui_router()` |
| `static` | static file prefixes | `registry.static_dir()` |
| `projections` | projection names | `registry.projection()` |
| `hooks` | `packet_screen`, `overview_panel`, `template_dir`: hooks without an identifier | those hooks |

### Identity and identifiers

Everything a plugin registers starts with its identity, unless it is listed in `legacy_ids`:
- **Plain identifiers:** the identity itself, or the identity followed by `_`, `.`, `-`, `:` or `/`, e.g.
  `ballots`, `ballots_queue` or `ballots.close`.
- **Routes:** the same, after a leading `/`, e.g. `/ballots` or `/ballots/item`.

A longer word is not the identity: `ballotsx` does not belong to `ballots`. An identifier that is neither
namespaced nor listed in `legacy_ids` is refused. A legacy identifier must also be listed in `provides`.

Legacy identifiers exist so that nothing saved has to change. A plugin named `ballots` that predates the rule
keeps its inbox kinds `round` and `tally`, its command kinds `vote.cast` and `vote.submit`, and its route
`/review`.

Renaming a persisted identifier is a migration, not an edit:
1. The new version resolves an alias (old to new) for at least one major version.
2. The plugin's migration rewrites open state (task `job.adapter`, open inbox items, queued commands). Applied
   history stays as written.

An alias is forward compatibility only. Going back needs the pre-upgrade snapshot restored, or a reverse migration
tested like the forward one.

### The framework range

`framework` is a PEP 440 range, compared by version number: `>=1.9` includes 1.10.0. A plugin whose range does
not include the installed framework is refused before any of its code runs. The reason names the plugin, its range
and the framework version.

### Configuration

A plugin owns `plugins.<identity>` in `config/routing.yaml`. Its pydantic `schema` validates that section
before `setup()` runs, so invalid configuration is a load failure that leaves nothing registered. It reads the
section with `plugin.config(layout, <identity>)`, or a part of it with `plugin.config(layout, <legacy section>)`.

`config.legacy_sections` names the top-level sections the values lived in before 1.12.0:
- **Reading:** until `supervisor migrate` has moved them, they are read from there, with a warning. The section
  named like the identity merges in at the top of `plugins.<identity>`, and any other nests under its own name.
  A plugin `render` listing `["render", "encoder"]` gets `encoder` as `plugins.render.encoder`.
- **Refused before import:** a legacy section the framework owns, and one that two manifests list.
- **Moving them:** `supervisor migrate` moves the legacy sections into `plugins.<identity>`, driven by the
  manifests alone; no plugin code runs. The shared `gpu` section moves to `resources.gpu` the same way. The moved
  lines keep their comments, the one on the section's key line included, and every other line of `routing.yaml`
  stays byte for byte. The move is refused, and nothing written, if the result does not read back as the same
  values.

## Gate G5: `supervisor plugin check <name>`

Whether one plugin conforms to this standard, run from a project that has it. It checks conformance only and
never runs the plugin's own tests; the G5 workflow runs those beside it, against the oldest and newest framework
in the plugin's range. Each check prints `OK` or `FAIL`, and any `FAIL` exits 1.

| Check | What it proves |
| --- | --- |
| `manifest` | the manifest is found, read and valid, without importing anything |
| `framework` | the range includes the installed framework |
| `imports` | the plugin's source imports `supervisor.plugin` only (and `supervisor.testing` in its tests) |
| `import_side_effects` | importing the module in a fresh interpreter changes no shared registry |
| `registrations` | `setup()`, against a staging registry that is never committed, registers exactly what `provides` declares; the registry holds each declared dependency's manifest, configuration schema and origin, so a dependency's function called from `setup()` reads its own configuration |
| `idempotent` | a second `setup()` registers the same |
| `config` | the project's section validates against the manifest's schema |

A plugin without a manifest fails `manifest`, and the rest is reported as not run. So is the rest of a plugin
the project's approval refuses (below): G5 imports none of its code, in its own process or in the fresh one, even
when another project in the process has it imported already. The fresh interpreter `import_side_effects` starts
takes the project's approval and refusal guard before its first import, as the loader does.

## Scaffolding

- `supervisor plugin new <name>` writes `plugins/<name>.py` and its manifest `plugins/<name>.toml`, and
  lists the name in `project.yaml`.
- `supervisor plugin new <name> --package [--dir <folder>]` creates `supervisor-plugin-<name>/`, the standard
  package, from `supervisor/plugin_template/`:
  - `pyproject.toml`, with the entry point `<name> = supervisor_plugin_<name>`, the manifest as package data, and pytest
    settings of its own;
  - the package: `setup()`, the manifest, and a pydantic `config.py`;
  - `tests/test_conformance.py`, which runs `supervisor plugin check <name>` in a temporary project;
  - a behaviour test;
  - `README.md` and `CHANGELOG.md`.

  The framework is not a declared dependency, since it is on no package index: the manifest's `framework` range,
  this minor release up to the next major, is what the loader checks.

Both shapes pass `supervisor plugin check` as generated, and the package's own suite passes as generated.
`<name>` is the identity: lower case letters, digits and `_`, starting with a letter. An existing folder is never
overwritten.

## Where a package lives

A package has one of two homes, and the standard is the same in both:
- **The framework repository's `packages/<distribution>/`** (from 2.0.0), for the packages the framework's
  maintainers keep: `packages/supervisor-plugin-textgen/` with its own `pyproject.toml`, version, CHANGELOG and tests.
  - It is an optional install. The framework's wheel holds `supervisor*` only, so nothing under `packages/` reaches
    a project unless the project installs that package's wheel.
  - Each release is tagged `<identity>-vX.Y.Z` (`text_model-v1.0.0`), with its wheel and sha256 on that tag's
    release. Only `vX.Y.Z` tags are framework releases: the update check, doctor's runtime check, G2's previous
    contract and the skills history never take a package's tag for one.
  - CI runs a job per package (`tools/ci_package.py`): the package's wheel and the framework's, built from the
    oldest framework release in the manifest's `framework` range and from HEAD, installed in a fresh environment
    with no source on the path; then the installed proof (found, loaded, G5, a task on what the pyproject's
    `[tool.supervisor.ci]` names: `lane`, `python_job`, a `config` table for its section) and the package's suite.
  - G1d (no project information) covers `packages/` like every other file in the repository. G1a to G1c, the
    framework's own import and literal rules, do not apply to a package: G5 holds its imports to `supervisor.plugin`.
  - A package promoted from a project keeps its identity, which may be one of that project's words. The local
    word list allows it for that package alone (`[projects.<name>.packages]`, below); everywhere else in the
    repository the word is still a hit.
- **A repository of its own**, for anyone else's package: tagged `vX.Y.Z` there, released and tested the same way
  (the plugin-release skill).

Either way the package and its built wheel hold no project's information: `supervisor boundary words <folder>
<wheel>` runs G1d's local word list over them, file names and the wheel's `METADATA` included. A release checks it
(plugin-release). A word that is the package's own public name is allowed there by the local file, never in the
repository:

```toml
[projects.<name>.packages]
supervisor-plugin-<name> = ["<word>"]     # allowed inside packages/supervisor-plugin-<name>/ and its wheel or sdist only
```

## Migrations

`migrations = "<module>"` names a module exposing `MIGRATIONS`, a list of `migration(layout, dry_run) -> list[str]`
under the same contract as the framework's:
- report only real changes;
- write nothing on a dry run;
- report nothing on a second run;
- change only what the note names.

`supervisor migrate` runs them after the framework's own migrations, importing only that module and never the
plugin's `setup()`. Notes are prefixed with the identity. A failing migration stops the run with an error naming
the plugin. A plugin whose framework range excludes the installed framework is skipped; it does not load
either. The plugin that owns an identifier or a section owns its migration and its rollback (`migration-author`). It also gets a read-only view of the shared resource
policy it declares in `requires.resources` (`resources.gpu`) through `plugin.resources()`. It never reads another
plugin's section. When it needs another plugin's behaviour, it calls that plugin and lists it in
`requires.plugins`.

## Where the manifest lives

| Plugin | Manifest |
| --- | --- |
| Project-local module `plugins/<name>.py` | `plugins/<name>.toml` beside it |
| Project-local package `plugins/<name>/` | `plugins/<name>/supervisor-plugin.toml` |
| Installed package | `supervisor-plugin.toml` inside the Python package, shipped as package data. The distribution declares an entry point in the group `supervisor.plugins` (`<identity> = <package>`) |

`supervisor/discovery.py` finds each name in `project.yaml: plugins` and reads its manifest without executing
anything the plugin ships:
- **Installed packages:** the manifest is read from the distribution's file list. For an editable install, whose file
  list shows only its finder, it is read beside the package that `importlib.util.find_spec` locates for the entry
  point's top-level package; finding that does not run it.
- **Two sources:** a name found both in `plugins/` and as an installed entry point is refused, and so is a name
  installed twice. Remove one, or rename one.
- **The identity:** a manifest whose `identity` differs from the name in `project.yaml` is refused.
- **No manifest (1.12.x):** a project-local plugin, or an installed module without an entry point, still loads,
  with a warning. A later major release requires the manifest.

## Approval before import

Every configured plugin is discovered first. What its manifest decides is settled before any plugin module is
imported, so a refused plugin never executes. Refused:
- a plugin whose `framework` range excludes the installed framework;
- a manifest that claims an identifier built into the framework (a lane such as `claude_cli`, a command kind such as
  `loop.pause`, an inbox kind such as `gate`);
- an identifier two manifests claim: both plugins are refused, and each message names the other;
- a plugin whose `requires.plugins` names one that `project.yaml` does not list, and every plugin in a
  cycle of them.

Plugins load in `project.yaml`'s order, except that each one moves after the plugins it requires. One whose
requirement is refused is refused too, and one whose requirement did not load is refused before its import. A refusal
counts as a load failure: a required plugin's refusal blocks the loop.

The same approval decides what `supervisor migrate` and G5 may import: a refused plugin directs no configuration
move, and none of its migrations runs.

A refused plugin's modules cannot be imported by any path the framework sets up, for the rest of the process:
- a sibling's `from . import <refused>`;
- the `plugins/` package initializer;
- a configuration schema or migration module;
- an installed plugin's entry-point module.

The import fails with ImportError, saying the module belongs to a refused plugin. This closes the import paths,
and is not a sandbox: an accepted plugin is trusted code. Importing a file under another name (for example
`plugins.bad` with the project on `sys.path`) is not covered.

A refusal belongs to the project whose approval made it. In a process with several projects, an installed
plugin one project refuses still loads for a project that accepts it:
- imports made for a project (its load, G5, `supervisor migrate`) follow that project's approval;
- outside any project (a lazy import at run time), a refused installed module stays closed unless a project in the
  process accepts it;
- discovery's spec lookup for an editable install, which imports nothing, is never blocked, so one project's refusal
  cannot hide a package's manifest from another project's approval.

A module already imported is not looked up again, so the loader and G5 check a refusal themselves before any
`setup()`.

## Loading: all or nothing

`setup(registry)` registers into a staging area. The process-wide tables (lanes, python jobs, command kinds) are
written only by one commit after `setup()` returns. Until then nothing outside the plugin can see its
registrations, not even another thread. The project's registry is published only once the whole load has finished:
another thread asking for it meanwhile waits for the load, and `plugin.config()` inside `setup()` reads the load in
progress. A load that does not finish (a `setup()` raising SystemExit or KeyboardInterrupt, or an error outside an
plugin's own load) rolls the interrupted plugin back and is published as blocking, never as complete: the
loop and the packet screens refuse to proceed until the process restarts.

Whatever fails, nothing the plugin registered remains:
- an exception in `setup()`;
- a registration its manifest does not declare in `provides`;
- a manifest that discovery refuses;
- an import error.

The project's registry is put back as it was, and the module that failed is dropped from `sys.modules`. This holds
for required and optional plugins alike. A required plugin that fails still blocks the loop, as before.

Doctor check names are known only when the checks run, so `provides.doctor_checks` is compared when `supervisor
doctor` runs them: a check reporting a name its manifest does not declare is a FAIL.

## Doctor and migrate

`supervisor migrate` sets no plugin up. Its migrations need only the manifests, so a project whose plugins
fail to load can still be migrated. `supervisor doctor` reads every configured plugin's manifest without loading
it, in the check `plugin_manifests` (gate G6), so the check is there when loading fails too:
- **FAIL:** a manifest discovery refuses, or a framework range that excludes the installed framework.
- **WARN:** a plugin without a manifest, and configuration still in the old top-level sections (run
  `supervisor migrate`).
- **OK:** each plugin with its version and range.

Importing a plugin module must change nothing: `setup()` is the only place anything is registered. A module
that still calls `register()` or `@job` at import (deprecated since 1.11.0) writes past the staging area, and a
failure cannot take that back.
