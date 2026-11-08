# Contributing to the framework

This repository stays generic by rule, not by memory, and the rule is checked by code (`docs/BOUNDARY.md`). Read
this before opening a pull request; it tells you where a change belongs, which gates it has to pass, and under
what licence your contribution is accepted.

## The three layers, and the one-way dependency

Every piece of code or config lives in exactly one of three layers, and dependencies point one way:

**project → plugin packages → framework.**

| Layer | Holds | Never |
|---|---|---|
| Framework (this repo) | the loop, store, gates, review, verification, inbox, Control Center, generic lanes, migrations, the template project, the public plugin API | imports a project or a plugin; names a project's domain, paths, hosts or people; carries a project's config values as defaults |
| Plugin package (`supervisor_plugin_<name>`) | one capability several projects could use: a lane for a tool, a review page, a doctor check, its own config schema and tests | imports framework internals; reads another plugin's config; assumes a project's layout beyond its declared config |
| Project folder | `project.yaml`, `config/`, `agents/`, `state/`, its plan and runbooks, project-only plugins in `plugins/` | edits framework or installed package files; patches them at runtime |

A framework change never imports a project or a plugin. A plugin never imports anything from `supervisor` except
the public plugin API. A project never edits a framework or plugin file; it configures and extends.

## The rules for the hard cases

The full set is `docs/BOUNDARY.md`'s "Rules for the hard cases"; the two that most often decide where a
contribution goes:

1. **Would a project about something completely different want this?** Yes → framework. Some projects → a plugin
   package. Only this one → a project's own folder. This is the question your pull request answers in writing
   (G7, below).
2. **A new hook needs a second user.** A hook request names a generic use beyond the project asking for it; with
   only one user, the project solves it in its own plugin. If you are proposing a new hook, name its second user
   in the pull request or expect it to be asked for.

## Which gates apply

Every gate is a command that fails loudly (`docs/BOUNDARY.md` has the full table, each gate's checks and since
when). "Gates green" for a contribution means the gates that apply to the kind of change it is — an excluded
gate is excluded for a stated reason, never silently skipped:

| Kind of change | Gates that apply | Excluded, and why |
|---|---|---|
| A framework change (`framework-change`, `framework-release`) | **G1** boundary check, **G2** plugin API contract, **G3** framework suite, **G7** contribution review; **G4** release, for a release | **G5** and **G6** apply only through the reference plugins G3 already loads |
| A plugin package (`plugin-new`, `plugin-promote`, `plugin-release`) | **G5** plugin conformance (across the package's declared framework range), **G6** as the project's own doctor, **G7** for the package's own review | **G1**–**G4**: they gate the framework itself, which these do not change — a needed hook is a framework change, made first. A package kept under this repository's own `packages/` still gets G1d, which covers every file there |
| A review of either (`boundary-review`) | every gate the reviewed change needs, re-run, and **G7**'s questions answered | — |

## Review: you first, then the maintainers

**You review your own work first.** Run the `boundary-review` skill (or walk its checklist by hand) and get
every gate that applies green *before* opening a pull request. The pull-request template is that checklist: a
written answer to "would a different project want this?", a new hook's second user, API/migration/docs impact,
and which gates you ran with their output.

**The maintainers always rerun every gate and review before merging**, for a framework change and a catalog
listing alike — your own green run is not taken on trust, and it never should be. A pull request that skips its
own checklist still has to pass theirs; doing the checklist first just means you find out sooner.

## Licence

This project is released under the **Apache License 2.0 with the Commons Clause**: anyone, companies included,
may use, modify and share it; nobody may sell it, or a product or service whose value comes entirely or
substantially from it (hosting and paid support included). It is source-available rather than open source in the
formal sense.

**There is no contributor licence agreement.** The licence will not change, and every contribution is accepted
under that same combined licence (Apache License 2.0 with the Commons Clause) as it stands in `LICENSE` — opening
a pull request is your agreement to that. Plugin packages keep their own authors' own licences; the catalog index
records a package's licence, and never approves one.

## Where to start

- `docs/FRAMEWORK_VS_PLUGIN.md` for the shorter version of the boundary.
- `docs/BOUNDARY.md` for the full gate table, the layer model and the rules for the hard cases.
- `docs/PLUGIN_STANDARD.md` and `docs/PLUGIN_API.md` if your change touches a plugin or the plugin surface.
- The `.github/pull_request_template.md` checklist, filled in, is how you show the gates you ran.
