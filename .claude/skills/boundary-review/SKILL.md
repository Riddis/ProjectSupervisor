---
name: boundary-review
description: Review a framework or extension change against the boundary - the G7 contribution checklist, every gate that applies re-run, and the framework's rule that it holds no project information. Use when asked to review a pull request or release for the framework or an extension package, "boundary review", "/boundary-review", or as the contributor's own review before opening a pull request.
---

# Boundary review (gate G7)

Two people use this: the **contributor**, before opening a pull request (every gate green, every answer written),
and the **reviewer** (a maintainer or the independent G4 reviewer), who re-runs every gate and checks the answers
instead of trusting them.

## 1. Get the change

- A pull request or a pushed branch: `git diff <base>...<head>` and `git log <base>..<head>`, where `<base>` is the
  branch it targets (`origin/main`) and `<head>` its exact commit.
- Local work not yet committed: `git diff HEAD` and `git status` for new files. "The commit before" is then `HEAD`.

To run anything against the commit before without touching the checkout, export it into a scratch folder:
`git archive <commit> | tar -x -C <scratch folder>` (or `git worktree add --detach <folder> <commit>`). Never
`git stash`: the stash is shared with every other checkout of the repository.

Read the whole diff before the gates.

## 2. The questions (write each answer down)

1. **Would a different project want this?** Name the kind of project, and say what it would do with it. "Only one
   project needs it" means it is an extension, not a framework change.
2. **For a new hook, API or public function:** who calls it? A hook or API needs a second user, and why that user
   cannot do without it. Count the hooks (the rows of the hook table in `docs/FRAMEWORK_VS_EXTENSION.md`) before
   and after; a public function that is not a hook is not counted there. A function nothing calls is a finding.
3. **The API (G2):** does it add, change or remove a name in `supervisor.ext` or a documented section of
   `docs/EXTENSION_API.md`? Then the CHANGELOG's "Extension API" entry classifies it: compatible, (editorial) or
   (breaking), and the version follows.
4. **State and config:** does it change the shape of anything under a project's `state/` or `config/`? Then it has a
   migration (the migration-author skill): dry-run aware, idempotent, keeping unrelated data, with its rollback
   stated.
5. **Docs and the CHANGELOG:** are the docs it touches current (`docs/`, the template's comments, the skills), and
   does the CHANGELOG say what changed?
6. **No project information:** the framework repository never contains a project's files or information: no
   project's names, paths, identifiers, task or decision ids, or examples built from one. Examples use invented
   names.

## 3. The gates, re-run (never taken from the description)

- **G1** `python -m supervisor.boundary` from the framework root: OK.
  - G1a–c scan `supervisor/` only. Read `docs/`, `tests/`, `examples/` and the skills yourself for machine paths,
    hosts, user names and project names. A search to start from: the changed lines of
    `git diff <base> -- docs tests examples .claude`, for a drive path (`grep -E '[A-Za-z]:\\'`), `/home/`, `/Users/`, a
    host name and your own login name. Then read what it did not catch.
  - G1d needs the local project-terms file, one table per project:

    ```toml
    [projects.<project>]
    terms = ["<word>", "<another word>"]
    ```

    When the file exists and nothing is found, G1d prints nothing. "G1d ... did not run" means there is no file:
    create it and run again. CI has no file, so the reviewer runs G1d locally. It reads every file git tracks and
    every new file git does not ignore, file names included, so uncommitted work and `packages/` are covered too.
  - For an extension package and its built wheel: `supervisor boundary words <package folder> <wheel>`.
- **G2** `python -m supervisor.api_contract check`: OK, and the classification matches the change.
- **G3** the suite: `python -m pytest -q` locally (Windows: `tools\test_windows.ps1`, which writes
  `runs\pytest_windows.txt`; in a worktree with no `.venv`, pass `-Python <python.exe>`).
  - For a pushed head, also read CI's exact-head checks for Linux and Windows.
  - For local work, only your own platform has run. Say so; the other runs when it is pushed.
- **G5** for an extension: `supervisor extension check <identity>`. For a package, across its framework range;
  for a package under `packages/`, its CI jobs run that against the oldest framework and HEAD.
- **G4**, for a release: the version rule, the CHANGELOG's Migration and Rollback lines, the tag only after
  acceptance.

## 4. Read the change itself

- **Each new test must be able to fail:**
  - Run it against "the commit before" (section 1 says how). It must fail on an assertion, not an ImportError or a
    missing name.
  - Where the code under test exists before the change, also run it against a stub that returns a wrong answer: a
    test that passes then cannot fail.
  - For code the change adds, the commit before can only give an ImportError, so the stub is the probe that
    counts: replace the new function with one that returns a wrong answer, and the test must fail.
- **New literals:** every new path, host, name or vendor word is generic, or listed as an explicit exception in
  `boundary.toml`.
- **Failure paths:** for behaviour with one (a commit, a migration, a load), probe that path in a disposable
  project.
- **Errors raised:** the change documents and tests each error it raises.

## 5. The verdict

**ACCEPT**, **REVISE** or **REJECT**, with each finding:
- ranked P1 (blocks, and is a defect), P2 (blocks, and is a gap in a promise), or P3 (worth fixing, does not block);
  a failing gate is a P1;
- with file:line evidence, and a probe someone else can run.

A REVISE lists exactly what would make it an ACCEPT.

## Checks it ends with

- G1 (with G1d), G2 and G3 re-run: G3 locally, plus CI's exact-head checks for a pushed head. G5 for any extension.
- G7: every question in section 2 answered in writing.
- G4 too, for a release.
