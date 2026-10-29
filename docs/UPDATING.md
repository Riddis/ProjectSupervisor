# Updating the framework under running projects

Projects install the framework **editable** from a local clone (`pip install -e <Supervisor>[ui]`), so a framework
commit is live in every project on that machine the next time a process starts. That is the point — one fix, every
project — and it is also why the framework carries a version and migrations.

## As a project owner

You normally do nothing by hand. When the Control Center launches (`start_control_center.bat`) it fetches the
framework's `origin`, prints the commits your clone is behind, and asks `Update now? [y/N]` in its window; the
same appears as a banner on the Overview with an **Update now** button while it runs (re-checked every 30 min).
Saying yes does, in order: if a loop is running, `loop.stop` (it stops at its next safe point and releases
`state/.lock`; the updater waits up to two minutes for that — nothing is pulled while anything holds the lock,
because `supervisor migrate` refuses then), `git pull --ff-only` in the framework clone, `pip install -e`
(dependencies may have changed), `supervisor migrate` and `supervisor doctor` in a child process on the new code,
the loop started again through `supervisor.launcher` (the restart-on-reload wrapper) if one was stopped, and
finally the Control Center restarts itself (exit code 3, which `tools/run_control_center.ps1` turns into a
relaunch). A clone with uncommitted changes is never pulled over; the banner says to commit or stash first.

Every step must succeed. If the reinstall, the migration or a doctor check that passed before the update fails,
the update reports **failed** with the reason and what to do (typically: fix the cause, then `supervisor
migrate`, `supervisor doctor`, and press Start) — the loop is not started on a project that did not migrate, and
the Control Center does not restart. The framework clone is then already on the new code (a pull is not undone);
`supervisor doctor` shows the mismatch until `supervisor migrate` has run. If the loop does not release the lock
within the timeout, nothing is changed: stop it (Stop button or `supervisor stop`) and run the update again.

The same by hand, from the project folder:

```
.venv\Scripts\python.exe -m supervisor update --check     # list what origin has
.venv\Scripts\python.exe -m supervisor update             # asks, then applies
.venv\Scripts\python.exe -m supervisor update --yes       # unattended
```

Or fully manual: `git -C <framework> pull`, then `supervisor doctor` (reports the version mismatch),
`supervisor migrate --dry-run`, `supervisor migrate`, `supervisor render`, Reload in the Control Center, restart
the UI. `supervisor run` never prompts (it may be unattended); it prints the available update and carries on.
Projects that are not touched keep working on the old version in their running processes and pick the new one up
on their next start.

If a release says a *major* migration is required, do not start the loop before `supervisor migrate` has run:
the doctor check for the project is FAIL until then, and the Control Center's Start button reports it. The same
check fails on a malformed `project.yaml` or one without `framework_version` (nothing then says which version
wrote the state); `supervisor migrate` records the installed version, and refuses on a malformed or missing file.

The Start button runs the loop through `python -m supervisor.launcher`, which restarts `supervisor run` whenever
it exits with code 3 (Reload, or a framework update) — the same convention as `tools/run_supervisor.ps1`, so a
loop started from the UI comes back on the new code too.

## As the framework author

The skills hold the step lists: `.claude/skills/framework-change` (where a change goes, test first, gates G1/G3/G7
before review), `framework-release` (version, CHANGELOG, review, tag: gate G4) and `migration-author` (any state or
config format change). `docs/BOUNDARY.md` describes the gates. The rules:

- Every change has a test. `python -m pytest -q` is green on Linux and Windows before a version bump; CI runs it,
  and `python -m supervisor.boundary` (G1), on every pull request.
- `supervisor/__init__.py` `__version__` and `pyproject.toml` `version` move together.
- A change to the shape of anything under a project's `state/` or `config/` gets a migration in
  `supervisor/migrations.py` (idempotent, honours `dry_run`) and a **Migration** line in `CHANGELOG.md`.
- `supervisor/template/` is the only place project files are born; keep it generic (`tests/test_framework.py`
  checks it) and keep its comments current — they are the documentation most owners actually read.
- Extension hooks are an API: adding one is a minor bump; changing one is a major bump with a migration note
  telling extension authors what to change.

## Multiple machines / a friend's machine

Clone the framework, create the project venv, install editable, done — the project folder carries no framework
code. If two machines run the same project (shared via git), both must run the same framework version; the
doctor check names the mismatch. Pin with a git tag when that matters: `git -C Supervisor checkout v1.1.0`.

## Pinned checkouts (the recommended setup since 1.8.0)

Since 1.8.0 `supervisor doctor` FAILs unless the framework checkout a project runs is clean and exactly at a tag, so
the updater above (which pulls a branch, normally onto an untagged commit) suits a development clone, not a live
project. A live project runs a separate worktree of the framework clone, detached at a release tag
(`git -C <Supervisor> worktree add <Supervisor-runtime> vX.Y.Z`), installed editable from there. It takes a new
release in a maintenance window: drain until no job record is open and no command is queued, stop the loop and
the Control Center, `git -C <Supervisor-runtime> fetch origin tag vX.Y.Z` (the update check reads origin's tags
without creating them locally), `git -C <Supervisor-runtime> checkout vX.Y.Z`, reinstall only if the dependencies changed,
`supervisor migrate --dry-run`, `supervisor migrate`, `supervisor doctor`, start.

Since 1.13.0 the project's skills come along: after `supervisor migrate`, `supervisor skills status` lists the
skills the new release ships, and `supervisor skills update` installs the missing ones and replaces the outdated
ones (never one the project edited, which it flags for a merge by hand), committing what it wrote. The project's
`project-upgrade` skill holds the whole window.

Since 1.12.0 the updater recognises such a checkout (detached at a `vX.Y.Z` tag) and never pulls it. At launch, in
`supervisor update --check` and on the Control Center's overview, it reports a newer release that origin publishes.
- **Which releases count:** both the release the checkout runs and the newer one come from origin's own tag list
  (`git ls-remote --tags`, each tag peeled to its commit), compared by version number.
  - The release the checkout runs is the newest one origin publishes at its commit.
  - A tag that exists only locally is not a release, wherever it points. An example is one made in the development
    clone that the runtime worktree shares its tags with.
  - When origin publishes no release at the checkout's commit, the check says so and announces nothing. A detached
    checkout that is not at a release tag at all is reported as such, and `supervisor doctor` fails it.
- **Migration notes:** only the newer release's objects are fetched, to read whether its changelog mentions a
  migration. No local tag is created, so a maintenance window fetches the tag before checking it out.
- **Without a fetch:** the overview reads the check without fetching, so it shows the last result taken from origin,
  with the time of that check. A failed refresh does not erase that result; the overview notes that the latest
  refresh failed.
- **Refusal:** `supervisor update` and the update route refuse a detached checkout and change nothing, whatever
  update check they were given. An update check is reused only while the checkout is still at the branch and commit
  it was taken of.

Before that, every launch on a pinned checkout printed "update check skipped: origin/HEAD unknown", because the
check looked for a branch.
