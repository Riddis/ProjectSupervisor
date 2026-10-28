---
name: extension-promote
description: Move a project-local extension into its own package (or a package's generic part into the framework) so other projects can use it - split out what is project-specific first, move the tests with the code, keep every saved identifier working, and switch the project to the package in the same change. Use when the user says "make this extension reusable", "promote this extension", "extract it into a package", "/extension-promote".
---

# Promoting an extension

Promotion makes one copy where there was one: after it, the code lives in the package (or the framework), the
project uses that, and nothing of the old copy is left behind. Nothing the project saved may break: tasks, job
records and inbox items name the extension's identifiers, so they keep resolving.

## 1. Split first

Read the extension and mark each part:
- **generic:** another project would want it as it is;
- **project-specific:** this project's names, paths, defaults, policy, data.

The project-specific parts stay in the project: as configuration the package reads from
`extensions.<identity>`, or as a small project-local extension that uses the package. Nothing project-specific goes
into the package. The package's repository carries no project's names, paths or examples either.

## 2. Create the package

1. Run the extension-new skill with `--package`, keeping the **same identity**, so saved identifiers still belong
   to it.
2. Move the generic code in. Every identifier the project saved stays registered under the same name, listed in
   `legacy_ids` when it predates the namespace rule.
3. Move the tests that test the generic behaviour with the code; the project keeps the tests of its own parts.
4. The package's config schema takes the values the project configured. An old section moves with a migration:
   the manifest's `config.legacy_sections`, which `supervisor migrate` moves, or the package's own migrations (the
   migration-author skill, with its rollback stated).

## 3. Switch the project, in one change

On a project branch:
- remove the old module and its manifest from `extensions/`;
- install the package into the project's environment, pinned in the project's lock;
- list the identity in `project.yaml` (unchanged when it was already listed);
- move any configuration the migration does not move.

Then rehearse it on a copy of the project (the project's rehearsal skill), including an open item and a job record
that name the extension's identifiers. All must still resolve, and rollback A must restore the old copy.

## 4. Into the framework instead

When the generic part belongs in the framework itself (every project wants it), this is the framework-change skill,
with the same rules: one copy afterwards, the project switched in the same release window, identifiers kept.

## Checks it ends with

- `supervisor extension check <identity>` (G5) in the project, now finding the installed package.
- The package's own suite, across its declared framework range.
- The project's suite, after the switch, on the rehearsal copy and then in the window.
- `grep` for the old module's name: no copy left in the project.
