---
name: plugin-promote
description: Move a project-local plugin into its own package (or a package's generic part into the framework) so other projects can use it - split out what is project-specific first, move the tests with the code, keep every saved identifier working, and switch the project to the package in a maintenance window. Use when the user says "make this plugin reusable", "promote this plugin", "extract it into a package", "/plugin-promote".
---

# Promoting a plugin

Promotion makes one copy where there was one: after it, the code lives in the package (or the framework), the
project uses that, and nothing of the old copy is left behind. Nothing the project saved may break: tasks, job
records and inbox items name the plugin's identifiers, so they keep resolving.

## 1. Split first

Read the plugin and mark each part:
- **generic:** another project would want it as it is;
- **project-specific:** this project's names, paths, defaults, policy, data.

The project-specific parts stay in the project: as configuration the package reads from
`plugins.<identity>`, or as a small project-local plugin that uses the package. Nothing project-specific goes
into the package. The package's repository (or its folder under the framework repository's `packages/`) carries
no project's names, paths or examples either.

## 2. Create the package

1. Run the plugin-new skill with `--package`, keeping the **same identity**, so saved identifiers still belong
   to it. Its G5 check runs in the package's own test project, not in the live project: while the local module
   exists, an installed package of the same name is refused there as "found in two places". The scaffold registers
   example identifiers (a `<identity>_hello` job, a doctor check) with tests that name them: remove the examples
   and their tests unless the package keeps them on purpose.
2. Move the generic code in. Every identifier the project saved stays registered under the same name, listed in
   `legacy_ids` when it predates the namespace rule.
3. Move the tests that test the generic behaviour with the code. The project keeps the tests of its own parts:
   when every project-specific part has become configuration, that is a test of the project's configuration of the
   package. A project test that loads `plugins/<identity>.py` goes in the same change as the module, or the
   project's suite fails after the switch.
4. The package's config schema takes the values the project configured. The module's former constants become keys
   of the same section: write the project's values into its config in the same change that removes the module, or
   the package runs on its defaults without a word. An old section moves with a migration: the manifest's
   `config.legacy_sections`, which `supervisor migrate` moves, or the package's own migrations (the
   migration-author skill, with its rollback stated).
5. Release it (the plugin-release skill), so the project installs one identified artifact. A promoted package
   starts at `1.0.0`: a project has already saved its identifiers, so they are meant to stay.

## 3. Rehearse the switch

First prepare the project's code change on a branch, without touching the live working tree:
`git worktree add -b <branch> <folder>` from the project, then remove the old module and its manifest there, write
the configuration (section 2, step 4) and commit. The rehearsal runs that branch, as the window will.

On a copy of the project (the project's rehearsal skill), with the copy's own environment, never the live one:
1. check out the branch in the copy (the old module and its manifest leave `plugins/` with it);
2. install the package's released wheel;
3. keep the identity in `project.yaml`, and move any configuration a migration does not;
4. `supervisor check`: no warning that an open task's job names a lane or python job nothing provides (it
   resolves each one against the framework's names and each plugin's manifest, here the installed package's).
   An open job record would be resolved only when the loop runs it, so the window drains first and leaves none
   open. Reading is enough: running an open task makes it rollback B;
5. roll the copy back: restore the snapshot, put the old module back, **and uninstall the package**, or the loop
   finds the identity twice and blocks. In a venv, `pip uninstall <distribution>`. In a target folder on
   `PYTHONPATH` pip refuses ("outside environment"): delete the files listed in
   `<folder>/<distribution>-<version>.dist-info/RECORD`, then that folder. Then doctor and the suite.

## 4. Switch the project, in a window

The project's environment is shared by every branch, so the package is installed in a maintenance window (the
project's maintenance-window skill), never ahead of it:
- the branch prepared in section 3 (old module and manifest removed, configuration written) is merged in the window;
- the package is installed, pinned in the project's lock if it has one (a new project may not have a lock yet:
  record the exact wheel and its sha256 in the project's runbook instead, `docs/runbooks/<project>.md` with the
  `name` from `project.yaml`, spaces as hyphens; the template ships none, so create it);
- `supervisor migrate`, `supervisor doctor`, the suite;
- the window's README says that rollback A also uninstalls the package, and how for the project's environment
  (section 3, step 5).

## 5. Into the framework instead

When the generic part belongs in the framework itself (every project wants it), this is the framework-change skill,
with the same rules: one copy afterwards, the project switched in the same release window, identifiers kept.

## Checks it ends with

- `supervisor plugin check <identity>` (G5) in the project, now finding the installed package.
- The package's own suite, across its declared framework range (the plugin-release skill says where those
  framework versions come from).
- The project's suite, after the switch, on the rehearsal copy (switched and rolled back) and then in the window.
- No copy left: no `plugins/<identity>.py` or `plugins/<identity>/` in the project. The identity itself rightly
  stays in `project.yaml` and the config.
- Nothing of the project in the package: `supervisor boundary words <package folder> <wheel>` (framework 1.14.0
  or later) runs the local project-terms file over both, file names and the wheel's `METADATA` included; it fails
  when the file is missing. Then read for what a word list cannot catch: the project's paths, aliases and example
  values. Examples use invented names.
- `supervisor check` in the project: no warning about a job naming the plugin's lanes or python jobs.
