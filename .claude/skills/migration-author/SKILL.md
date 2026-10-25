---
name: migration-author
description: Write a migration for any persisted format change - framework (supervisor/migrations.py), extension or project - so it is dry-run aware, idempotent, safe to interrupt, keeps unrelated data, and has a stated rollback. Use when a change alters the shape of anything under a project's state/ or config/, renames a persisted identifier, or the user says "write a migration", "/migration-author". Called from framework-change and framework-release; written once, never copied.
---

# Writing a migration

A migration changes files that hold a project's history and configuration, usually while nobody is watching the
result line by line. It must therefore do exactly what its note says, and nothing on a second run.

## The contract

A framework migration is a function `(layout, dry_run) -> list[str]` in `supervisor/migrations.py`, appended to
`MIGRATIONS`; `supervisor migrate` runs them all, then records the installed version in `project.yaml`. Each one:

1. **Reports only real changes.** Compute the new text or document, compare it with the old, and return a note
   only when they differ. Never report a file because it merely mentions the old value (a note, a comment).
2. **Honours `dry_run`.** With `dry_run=True` it returns the same notes and writes nothing.
3. **Is idempotent.** A second run returns `[]` and writes nothing.
4. **Changes only what it names.** Rewrite the field, not every occurrence of a word; keep comments, key order
   and unrelated data. Write through `store.rewrite_text()`, which writes only on a change and keeps the file's
   LF or CRLF line endings (`Path.write_text` turns LF into CRLF on Windows).
5. **Survives interruption.** Each file is written whole (no partial rewrite) and the migration can run again over
   a half-migrated tree and finish the job — rule 3 makes that true when every file is judged on its own.
6. **Fails clearly.** An unreadable file is skipped with a note or left to `supervisor doctor`, never "fixed" by
   guessing; a project it cannot migrate raises `MigrationError` saying what to do.
7. **Leaves applied history alone.** Open state (tasks, open inbox items, queued commands, job records that will
   run) moves to the new format; history that is only read stays as it was written, and readers keep accepting it.

## Tests (in the same change)

- A fixture in the **old** format, written as bytes so line endings are part of the test (one LF file, one CRLF
  file), including a file that mentions the old value without using it.
- `migrations.run(layout, dry_run=True)` returns exactly the expected notes and changes no byte.
- `migrations.run(layout)` produces the new format; the task store and the loop load it.
- A second `run` returns `[]`; unrelated fields, comments and line endings are unchanged.
- For a rename of a persisted identifier: an alias that resolves the old name for at least one major version.

## Rollback (stated in the CHANGELOG's Migration line)

An alias gives forward compatibility only: the old version does not know the new identifiers a migration wrote.
Going back therefore needs one of:

- **snapshot restore**: the matching pre-upgrade snapshot of `state/`, `config/` and the environment, taken in
  the maintenance window before `supervisor migrate` (the project's rehearsal and restore procedures); or
- **a reverse migration**, tested like the forward one (old-format result from a new-format fixture, idempotent).

Once work has run on the new version, reconcile before any restore: the work done since the upgrade is applied
to the restored state or explicitly discarded by the owner (rollback A, before any work ran, versus rollback B).

## Extension and project migrations

The same contract applies to an extension's migration of its own identifiers and config section (from 1.11.0
through the extension standard) and to a project's own state changes. The extension that owns an identifier owns
its migration and its rollback path; a project migration runs inside the project's maintenance window with a
snapshot taken first.
