# The extension standard

One shape for every extension, so any project can install one and any contributor can read one. A project-local
extension follows the same rules as a packaged one, minus the packaging; promoting it is then a move, not a rewrite.
`EXTENSION_API.md` is what an extension may call; this document is what an extension must declare. Both are part of
the boundary (`BOUNDARY.md`); the standard arrives with 1.12.0 (boundary plan phase 3).

Three names are kept apart:
- **The identity**: the stable name in the manifest, which saved state refers to. Only this matters to the framework.
- **The Python package or module name.**
- **The distribution name.**

For example, the Ollama lane's identity is `local_llm`, because tasks and job records already name that adapter. Its
package could be `supervisor_ext_ollama` and its distribution `supervisor-ext-ollama`.

## The manifest: `supervisor-extension.toml`

Read by the framework before anything the extension ships is imported (`supervisor/manifest.py`). The schema and
migrations are module names here, imported only once the extension is approved.

```toml
identity = "local_llm"                       # required; lower case letters, digits and _, starting with a letter
version = "1.0.0"                            # required; the extension's own version (PEP 440)
framework = ">=1.12,<2"                      # required; the framework versions it supports (a PEP 440 range)
migrations = "supervisor_ext_ollama.migrations"   # optional; a module exposing MIGRATIONS

[provides]                                   # what setup() registers; nothing else may be registered
adapters = ["local_llm"]
doctor_checks = ["local_llm_server", "ollama"]
hooks = ["overview_panel"]                   # hooks that register something without an identifier of its own

[legacy_ids]                                 # identifiers older than the namespace rule, kept as they are
doctor_checks = ["ollama"]

[config]                                     # optional; the extension's own configuration section
section = "extensions.local_llm"             # always extensions.<identity>
schema = "supervisor_ext_ollama.config:Settings"   # module:Class, a pydantic model of that section
legacy_sections = ["local_llm"]              # optional; the top-level sections its values came from before 1.12.0

[requires]                                   # optional
resources = ["gpu"]                          # shared resource policy it reads (resources.gpu)
services = ["http://127.0.0.1:11434"]        # what it talks to, shown by doctor
extensions = ["comfy"]                       # identities it calls into; they load first
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

Everything an extension registers starts with its identity, unless it is listed in `legacy_ids`:
- **Plain identifiers:** the identity itself, or the identity followed by `_`, `.`, `-`, `:` or `/`, e.g.
  `review_rounds`, `review_rounds_queue` or `review_rounds.close`.
- **Routes:** the same, after a leading `/`, e.g. `/review_rounds` or `/review_rounds/item`.

A longer word is not the identity: `review_roundsx` does not belong to `review_rounds`. An identifier that is neither
namespaced nor listed in `legacy_ids` is refused. A legacy identifier must also be listed in `provides`.

Legacy identifiers exist so that nothing saved has to change. The Studio's review extension keeps its inbox kinds
`round`, `labelling` and `external_edit`, its command kinds `review.set_verdict` and `review.submit`, and its route
`/review`.

Renaming a persisted identifier is a migration, not an edit:
1. The new version resolves an alias (old to new) for at least one major version.
2. The extension's migration rewrites open state (task `job.adapter`, open inbox items, queued commands). Applied
   history stays as written.

An alias is forward compatibility only. Going back needs the pre-upgrade snapshot restored, or a reverse migration
tested like the forward one.

### The framework range

`framework` is a PEP 440 range, compared by version number: `>=1.9` includes 1.10.0. An extension whose range does
not include the installed framework is refused before any of its code runs. The reason names the extension, its range
and the framework version.

### Configuration

An extension owns `extensions.<identity>` in `config/routing.yaml`. Its pydantic `schema` validates that section
before `setup()` runs, so invalid configuration is a load failure that leaves nothing registered. It reads the
section with `ext.config(layout, <identity>)`, or a part of it with `ext.config(layout, <legacy section>)`.

`config.legacy_sections` names the top-level sections the values lived in before 1.12.0:
- **Reading:** until `supervisor migrate` has moved them, they are read from there, with a warning. The section
  named like the identity merges in at the top of `extensions.<identity>`, and any other nests under its own name.
  The Studio's `comfy` lists `["comfy", "ffmpeg"]`, so `ffmpeg` becomes `extensions.comfy.ffmpeg`.
- **Refused before import:** a legacy section the framework owns, and one that two manifests list.
- **Moving them:** `supervisor migrate` moves the legacy sections into `extensions.<identity>`, driven by the
  manifests alone; no extension code runs. The shared `gpu` section moves to `resources.gpu` the same way. The moved
  lines keep their comments, the one on the section's key line included, and every other line of `routing.yaml`
  stays byte for byte. The move is refused, and nothing written, if the result does not read back as the same
  values.

## Migrations

`migrations = "<module>"` names a module exposing `MIGRATIONS`, a list of `migration(layout, dry_run) -> list[str]`
under the same contract as the framework's:
- report only real changes;
- write nothing on a dry run;
- report nothing on a second run;
- change only what the note names.

`supervisor migrate` runs them after the framework's own migrations, importing only that module and never the
extension's `setup()`. Notes are prefixed with the identity. A failing migration stops the run with an error naming
the extension. An extension whose framework range excludes the installed framework is skipped; it does not load
either. The extension that owns an identifier or a section owns its migration and its rollback (`migration-author`). It also gets a read-only view of the shared resource
policy it declares in `requires.resources` (`resources.gpu`) through `ext.resources()`. It never reads another
extension's section. When it needs another extension's behaviour, it calls that extension and lists it in
`requires.extensions`.

## Where the manifest lives

| Extension | Manifest |
| --- | --- |
| Project-local module `extensions/<name>.py` | `extensions/<name>.toml` beside it |
| Project-local package `extensions/<name>/` | `extensions/<name>/supervisor-extension.toml` |
| Installed package | `supervisor-extension.toml` inside the Python package, shipped as package data. The distribution declares an entry point in the group `supervisor.extensions` (`<identity> = <package>`) |

`supervisor/discovery.py` finds each name in `project.yaml: extensions` and reads its manifest without executing
anything the extension ships:
- **Installed packages:** the manifest is read from the distribution's file list. For an editable install, whose file
  list shows only its finder, it is read beside the package that `importlib.util.find_spec` locates for the entry
  point's top-level package; finding that does not run it.
- **Two sources:** a name found both in `extensions/` and as an installed entry point is refused, and so is a name
  installed twice. Remove one, or rename one.
- **The identity:** a manifest whose `identity` differs from the name in `project.yaml` is refused.
- **No manifest (1.12.x):** a project-local extension, or an installed module without an entry point, still loads,
  with a warning. A later major release requires the manifest.

## Approval before import

Every configured extension is discovered first. What its manifest decides is settled before any extension module is
imported, so a refused extension never executes. Refused:
- an extension whose `framework` range excludes the installed framework;
- a manifest that claims an identifier built into the framework (a lane such as `claude_cli`, a command kind such as
  `loop.pause`, an inbox kind such as `gate`);
- an identifier two manifests claim: both extensions are refused, and each message names the other;
- an extension whose `requires.extensions` names one that `project.yaml` does not list, and every extension in a
  cycle of them.

Extensions load in `project.yaml`'s order, except that each one moves after the extensions it requires. One whose
requirement did not load is refused before its import. A refusal counts as a load failure: a required extension's
refusal blocks the loop.

## Loading: all or nothing

`setup(registry)` registers into a staging area. The process-wide tables (lanes, python jobs, command kinds) are
written only by one commit after `setup()` returns. Until then nothing outside the extension can see its
registrations, not even another thread. Whatever fails, nothing the extension registered remains:
- an exception in `setup()`;
- a registration its manifest does not declare in `provides`;
- a manifest that discovery refuses;
- an import error.

The project's registry is put back as it was, and the module that failed is dropped from `sys.modules`. This holds
for required and optional extensions alike. A required extension that fails still blocks the loop, as before.

Doctor check names are known only when the checks run, so `provides.doctor_checks` is compared when `supervisor
doctor` runs them, not at load.

Importing an extension module must change nothing: `setup()` is the only place anything is registered. A module
that still calls `register()` or `@job` at import (deprecated since 1.11.0) writes past the staging area, and a
failure cannot take that back.
