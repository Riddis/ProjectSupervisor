# The framework boundary and its gates

The framework stays generic only if the boundary is checked by code, not by memory. `FRAMEWORK_VS_EXTENSION.md`
says where a change goes; this document says how that is enforced: the layers, the rules that decide hard cases,
and the gates every change passes. The full plan, with its review record, is the owner's "Framework boundary and
extension standard plan" (2026-10-25); its rollout runs as framework releases 1.9.0 onward.

## Layers

Every piece of code or config lives in exactly one layer, and dependencies point one way:
**project → extension packages → framework**.

| Layer | Holds | Never |
|---|---|---|
| Framework (this repo) | loop, store, gates, review, verification, inbox, Control Center, generic lanes (agent CLIs, process, python), migrations, the template project, the public extension API (from 1.11.0) | imports a project or an extension; names a project's domain, paths, hosts or people; carries a project's config values as defaults |
| Extension package (`supervisor_ext_<name>`, from 1.12.0) | one capability several projects could use: a lane for a tool, a review page, a doctor check, its config schema and tests | imports framework internals; reads another extension's config; assumes a project's layout beyond its declared config |
| Project folder | `project.yaml`, `config/`, `agents/`, `state/`, plan and runbooks, project-only extensions in `extensions/` | edits framework or installed package files; patches them at runtime |

## Rules for the hard cases

1. **Would a project about something completely different want this?** Yes → framework. Some projects → extension
   package. Only this one → project.
2. **Public API only.** Extensions import the public extension API and nothing else from `supervisor` (enforced
   from 1.11.0 with deprecation warnings, then as a doctor FAIL). What an extension needs and the API lacks is a
   framework change, made first.
3. **A new hook needs a second user.** A hook request names a generic use beyond the project asking for it; with
   only one user, the project solves it in its extension.
4. **Values belong to projects, defaults to the framework.** A default that is only right for one project is a
   project value.
5. **Promotion is deliberate and one-way at a time:** project extension → package → framework, each step with its
   tests, never a copy kept in two places.

## Gates

Each gate is a command that fails loudly, so a person, an agent session or CI gets the same verdict. The skills
walk contributors through them; no gate depends on a skill being used.

| Gate | Checks | Runs | Since |
|---|---|---|---|
| **G1** boundary check | `supervisor boundary check` / `python -m supervisor.boundary` over `supervisor/` — (a) no static import of a project or extension module, dynamic loading only in the listed loader; (b) no drive-letter or home-directory path, non-loopback host, e-mail address or registered user name; (c) no model or vendor name outside the listed files. Exceptions are explicit entries in `boundary.toml`. (d), from 1.12.1: no project information anywhere in the repository (every tracked text file): the words of each project, listed in a local file outside every repository (`SUPERVISOR_PROJECT_TERMS`, else `~/.config/supervisor/project-terms.toml`), never in `boundary.toml`. Without that file (CI) G1d says it did not run | CI (required, G1a–c); `.githooks/pre-commit` locally (all four) | 1.9.0; G1d 1.12.1 |
| **G2** extension API contract | `python -m supervisor.api_contract check` — `supervisor.ext.__all__` (names, kinds, signatures, class members) equals `docs/extension_api.json`, every name has its own section in `docs/EXTENSION_API.md`, and the difference from the last release tag's contract (both files, read with `git show`) follows the version rule: a removal or a signature change that breaks callers only in a major version after a minor of deprecation; an addition or compatible change at least a minor version with a CHANGELOG "Extension API" entry naming it; a changed documented section named in such an entry and classified `(editorial)` (any version), `(breaking)` (major) or neither (compatible: at least minor) | CI (required) | 1.11.0 |
| **G3** framework suite | full suite on Linux and Windows, the template smoke test (`init` → `doctor` → one task to COMPLETE); the reference extension passes its hook contract tests and G5 (`tests/test_hook_contracts.py`) | CI on every push and pull request (required) | 1.9.0 |
| **G4** release | CHANGELOG, version rule, migrations (idempotent, dry-run aware), G2 against the last release, tag, independent review with the boundary checklist; runtime worktrees advance only to tags | the `framework-release` skill and the reviewer; `supervisor doctor` refuses an untagged checkout | 1.9.0 |
| **G5** extension conformance | `supervisor extension check <name>` (`docs/EXTENSION_STANDARD.md`): manifest, framework range, imports of `supervisor.ext` only, no registration at import, `setup()` registering exactly what the manifest's `provides` declares (and the same twice), config schema; it never runs the extension's tests, which the G5 workflow runs beside it across the extension's framework range | the extension's CI; framework CI job "G5 reference extension" (required: the reference installed as a wheel and as an editable install in a fresh environment, and a package generated by `extension new --package` running its suite); doctor's `extension_manifests` runs the static half | 1.12.0 |
| **G6** project compatibility | doctor and the loader: framework range, no internal imports, framework checkout clean and on a tag. From 1.11.0 the loader scans extension sources for imports from `supervisor` other than `supervisor.ext` / `supervisor.testing`: a `DeprecationWarning` and a doctor WARN (`extension_imports`) with file and line; a FAIL in a later major release. From 1.12.0 the loader decides each manifest's framework range and identifier collisions before importing anything, and doctor's `extension_manifests` reports them from the manifests alone (a WARN for an extension without one) | `supervisor doctor` at every start, the loop's start-up | today (doctor), extended in 1.11.0 / 1.12.0 |
| **G7** contribution review | a written answer to rule 1; for a new hook its second user; API, migration and docs impact | the contributor (pull-request template, gates green), then the maintainers rerun every gate | 1.9.0 (checklist in `framework-release`) |

### Which gates apply

"Gates green" means the gates listed here; an excluded gate is excluded for the stated reason, not skipped.

| Phase or skill | Gates that apply | Excluded, and why |
|---|---|---|
| 1.9.0 | G1, G3 without the extension part, G4 through `framework-release`, G6 as today's doctor, G7 | G2 and G5: built in 1.11.0 and 1.12.0 |
| 1.10.0, 1.10.1 | as 1.9.0: a feature release (and its patch) ahead of phase 2, which moved to 1.11.0 (extension packages to 1.12.0) | G2 and G5: built in 1.11.0 and 1.12.0 |
| 1.11.0 | G1, G2 (introduced: this release records the first contract), G3, G4, G6 with the internal-import warning, G7 | G5: built in 1.12.0 |
| 1.12.0 onward | all seven | — |
| `framework-change`, `framework-release` | G1, G2 (from 1.11.0), G3, G7; G4 for a release | G5 and G6 apply only through the reference extensions G3 loads |
| `migration-author` | the gates of the change it is part of, plus its own checklist | — |

## Running the gates locally

```
python -m supervisor.boundary                      # G1, from the framework root; no install needed
python -m supervisor.api_contract check            # G2; `record` rewrites docs/extension_api.json after an API change
python -m pytest -q                                # G3 (tools\test_windows.ps1 on Windows saves the log)
git config core.hooksPath .githooks                # once per clone: G1 before every commit
```

`boundary.toml` is part of the boundary: adding an exception, a vendor file or a dependency's import name is a
reviewed change, and its commit says why. A listed vendor file that no longer names a vendor is reported so the
list stays tight.

## Enforcement on GitHub

`.github/workflows/ci.yml` runs G1, G2 (from 1.11.0, with the full history and tags, so it can read the last
release's contract) and G3 (Linux and Windows) on every push to `main` and every pull request. Branch protection on
`main` makes them required checks, so a change that fails one cannot merge; that setting lives in the repository's
settings, not in a file (Settings → Branches → `main`: require a pull request and the status checks
`G1 boundary check`, `G2 extension API`, `G3 suite (ubuntu-latest)`, `G3 suite (windows-latest)`). A new check is
added there once it has passed on a pull request.
