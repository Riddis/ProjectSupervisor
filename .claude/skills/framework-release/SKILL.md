---
name: framework-release
description: Cut a Supervisor framework release (gate G4) - choose the version by rule, finish the CHANGELOG and migration notes, run the gates on the release commit, request the independent review with the boundary checklist, and tag only after it accepts. Use when the user says "release the framework", "cut 1.x.y", "tag a release", "/framework-release", or when framework-change work is merged and should reach projects.
---

# Releasing the Supervisor framework

A release is the only way framework code reaches a project: projects run a clean checkout exactly at a tag, and
`supervisor doctor` refuses anything else. A tag is therefore a promise that the gates passed and a reviewer
accepted this exact commit. Never tag before the review accepts; never move or reuse a tag.

## 1. Version by rule

From the changes since the last tag (`git log --oneline <last-tag>..main`, `CHANGELOG.md ## Unreleased`):

- **major**: a change that needs `supervisor migrate` before the loop may start, a changed or removed extension
  hook, a breaking change to the public extension API (from 1.11.0: only after one minor release of deprecation
  warnings);
- **minor**: a feature, a new hook, an addition to the public API;
- **patch**: fixes only.

`supervisor/__init__.py` `__version__` and `pyproject.toml` `version` move together, in the release commit.

## 2. Changelog and migrations

Rename `## Unreleased` to `## X.Y.Z — <date>` and make it complete for two readers: the project owner (what
changes for me, what must I do) and the extension author (hooks, API). Every state or config shape change has a
migration in `supervisor/migrations.py` written with **migration-author**, and a **Migration** line saying what
`supervisor migrate` does and how to roll back. "No state or config shape change" is said explicitly when true.
An "Extension API" entry for every addition, change or deprecation, naming each (G2 compares the contract with the last
tag's). A changed section of `docs/EXTENSION_API.md` needs one too, marked `(editorial)` (wording only), `(breaking)`
(major version) or unmarked (a compatible change: at least minor); the reviewer checks the classification.

## 3. Gates on the release commit

On the exact commit to be tagged (a clean checkout, nothing uncommitted — check `git status`):

- **G1** `python -m supervisor.boundary`: OK, no notes;
- **G2** `python -m supervisor.api_contract check` in a clone with full history and tags: the contract recorded in
  `docs/extension_api.json` is the code, every name has its own section in `docs/EXTENSION_API.md`, and the
  difference from the last tag's contract (signatures and documented sections) fits the version and is named in the
  CHANGELOG (it prints the comparison and each classification; a shallow clone is an error, not a pass);
- **G3** CI green on Linux and Windows for this commit; on the workstation also `tools\test_windows.ps1`, and keep
  `runs/pytest_windows.txt` for the review;
- **G5** (from 1.12.0) the CI job "G5 reference extension" green for this commit. A minor or major release also
  raises the reference extension's manifest range (`examples/reference_extension/reference/supervisor-extension.toml`)
  when the reference uses what this release adds;
- `supervisor migrate --dry-run` and `supervisor migrate` on a copy of a project at the previous version: the
  dry run lists exactly what the CHANGELOG says, the apply changes that, a second run reports nothing.

## 4. Independent review (G7 checklist)

Ask the reviewer (an independent reviewer for framework releases, never the author's own session) to review the
commit range against this checklist and return ACCEPT, REVISE or REJECT with findings:

- [ ] Every change answers rule 1 (would a different project want it?) in its commit or pull request; every new
      hook names its second user.
- [ ] `supervisor/` imports nothing from a project or extension; G1 passes and every `boundary.toml` change is
      explained.
- [ ] No project value became a framework default; template changes stay generic.
- [ ] Version follows the rule; CHANGELOG complete for owners and extension authors.
- [ ] Each migration is idempotent, honours `dry_run`, preserves unrelated data and line endings, resumes after
      an interruption, and has a stated rollback (snapshot restore or a tested reverse migration).
- [ ] Docs updated: `FRAMEWORK_VS_EXTENSION.md` for hooks, `BOUNDARY.md` for gates, template comments.
- [ ] Tests cover each change; the suite is green on both systems for this commit.

A REVISE produces a fix commit and a new review of the delta; the version stays the same until it is tagged.

## 5. Tag and announce

After ACCEPT: `git tag -a vX.Y.Z -m "X.Y.Z"` on the reviewed commit, push the branch and the tag. Projects take it
in a maintenance window (the procedure in **framework-change**, step 6, and the project's own runbook); say in
the announcement whether a migration runs and whether extensions must change.
