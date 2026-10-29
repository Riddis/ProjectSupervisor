---
name: project-upgrade
description: Take a new framework release (or a new plugin package version) into this project - read what it changes, rehearse it, move the framework checkout to the new tag in a maintenance window, migrate, bring the project's framework skills up to date, and start paused. Use when the user says "upgrade the framework", "take vX.Y.Z", "a new release is out", "/project-upgrade", or the Control Center reports a newer release.
---

# Taking a new framework release

A live project runs the framework from its own checkout, detached at a release tag, installed editable into the
project's environment (the framework's `docs/UPDATING.md`, "Pinned checkouts"). An upgrade moves that checkout to a
newer tag, only in a maintenance window.

Find the checkout with the project's own environment, from the project folder and without `PYTHONPATH` set (it
would point elsewhere):
`.venv\Scripts\python.exe -c "import supervisor, pathlib; print(pathlib.Path(supervisor.__file__).parents[1])"`
(`.venv/bin/python` elsewhere). `supervisor update --check` lists the newer releases origin publishes (it needs the
network).

## 1. Read the release first

From the new tag's `CHANGELOG.md` section, write down:
- the **Migration** line: what `supervisor migrate` will change in this project's files. No Migration line means
  none: migrate only records the version;
- the **Rollback** line: how to go back, and from which point a restore needs reconciling first. No Rollback line
  means rollback A of the window (the previous tag plus the snapshot);
- any changed dependency: `git -C <framework checkout> diff <current tag> <new tag> -- pyproject.toml`. If one
  changed, the environment is reinstalled from the project's lock, by the project's own install procedure;
- anything the project must change itself (a plugin using an API that changed, a config value that moved).

If the project must change too, that change is made and tested on a branch first, and merged in the window.

## 2. Rehearse

Run the rehearsal skill with the new tag, unless the release says it changes no state, config or files the
project relies on, and the owner agrees to skip it.

## 3. The window

Run the maintenance-window skill. Its change step is:
1. `git -C <framework checkout> fetch origin tag vX.Y.Z` (the update check reads origin's tags without creating
   them locally), then `git -C <framework checkout> checkout vX.Y.Z`. Check: `git -C <framework checkout> describe
   --tags --exact-match` prints the tag, and `git status` there is clean.
2. Reinstall only if a dependency changed.
3. Merge the project's branch for this release, if it has one. Its uncommitted changes must not overlap the branch
   (check with `git diff --name-only HEAD...<branch>` against `git status --porcelain`).
4. `supervisor skills status`, then `supervisor skills update`. The framework's newer skills come in and are
   committed. An unedited copy, or one an earlier release shipped, is replaced. An **edited** one is left: merge it
   by hand from the template file the status names:
   1. compare the project's copy with that file;
   2. keep the project's own additions, and take the template's changes;
   3. commit the merge.

The window's own end (migrate, doctor, the suite on live data, start paused) follows.

## 4. After

- `supervisor doctor`'s `framework` check names the new tag, and `supervisor update --check` says the checkout is
  pinned at the newest release.
- `git status` in the project shows only the work still in review: the loop commits its own records, and `migrate`
  and `skills update` committed theirs.
- Add the window's record to the project's runbook (`docs/runbooks/<project>.md`), and tell the owner the project is
  PAUSED for them to resume.

## Checks it ends with

- `supervisor doctor` (G6): OK, or every FAIL explained.
- The project's test suite on the live files: green.
- `supervisor skills status`: every framework skill current, or the edited ones named for the owner.
