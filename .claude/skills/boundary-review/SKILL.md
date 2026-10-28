---
name: boundary-review
description: Review a framework or extension change against the boundary - the G7 contribution checklist, every gate that applies re-run, and the framework's rule that it holds no project information. Use when asked to review a pull request or release for the framework or an extension package, "boundary review", "/boundary-review", or as the contributor's own review before opening a pull request.
---

# Boundary review (gate G7)

Two people use this: the **contributor**, before opening a pull request (every gate green, every answer written),
and the **reviewer** (a maintainer or the independent G4 reviewer), who re-runs every gate and checks the answers
instead of trusting them.

## 1. The questions (write each answer down)

1. **Would a different project want this?** Name the kind of project, and say what it would do with it. "Only one
   project needs it" means it is an extension, not a framework change.
2. **For a new hook or API:** what is its second user, and why can that user not do without it? Each hook is a
   cost: count the hooks in `docs/FRAMEWORK_VS_EXTENSION.md` before and after.
3. **The API (G2):** does it add, change or remove a name in `supervisor.ext` or a documented section of
   `docs/EXTENSION_API.md`? Then the CHANGELOG's "Extension API" entry classifies it: compatible, (editorial) or
   (breaking), and the version follows.
4. **State and config:** does it change the shape of anything under a project's `state/` or `config/`? Then it has a
   migration (the migration-author skill): dry-run aware, idempotent, keeping unrelated data, with its rollback
   stated.
5. **Docs:** are the docs it touches current (`docs/`, the template's comments, the skills)?
6. **No project information:** the framework repository never contains a project's files or information: no
   project's names, paths, identifiers, task or decision ids, or examples built from one. Examples use invented
   names.

## 2. The gates, re-run (never taken from the description)

- **G1** `python -m supervisor.boundary` from the framework root: OK.
  - G1d needs the local project-terms file (`docs/BOUNDARY.md`). If it says it did not run, create the file and run
    it again.
  - Without the file, CI cannot run G1d, so the reviewer runs it.
- **G2** `python -m supervisor.api_contract check`: OK, and the classification matches the change.
- **G3** the suite on Linux and on Windows. CI runs both: read the exact-head checks.
- **G5** for an extension: `supervisor extension check <identity>`. For a package, across its framework range.
- **G4**, for a release: the version rule, the CHANGELOG's Migration and Rollback lines, the tag only after
  acceptance.

## 3. Read the change itself

- Each new test fails without the change: run it against the commit before.
- Every new path, host, name or vendor word is generic, or listed as an explicit exception in `boundary.toml`.
- For behaviour with a failure path (a commit, a migration, a load), probe that path in a disposable project.

## 4. The verdict

**ACCEPT**, **REVISE** or **REJECT**, with each finding:
- ranked P1 (blocks, and is a defect), P2 (blocks, and is a gap in a promise), or P3 (worth fixing, does not block);
- with file:line evidence, and a probe someone else can run.

A REVISE lists exactly what would make it an ACCEPT.

## Checks it ends with

- G1 (with G1d), G2, G3 at the exact head, and G5 for any extension, all re-run.
- G7: every question in section 1 answered in writing.
- G4 too, for a release.
