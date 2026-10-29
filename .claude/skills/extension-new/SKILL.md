---
name: extension-new
description: Write a new Supervisor extension - decide whether it is project-local or a package, scaffold it in the standard shape, give it a manifest, identity, config schema and identifiers, and prove it with gate G5 and doctor. Use when the user says "write an extension", "add a lane/command/page/check as an extension", "new extension package", "/extension-new".
---

# Writing an extension

An extension is code the framework loads into a project through `supervisor.ext`, its only public API. Its shape is
`docs/EXTENSION_STANDARD.md`; its API is `docs/EXTENSION_API.md`. A change every project would want is not an
extension but a framework change (the framework-change skill).

## 1. Local or package (rule 1)

Ask: would a different project want this, as it is?
- **No:** it is **project-local**, a module in the project's `extensions/` folder.
- **Yes, or it will be shared:** it is a **package**, installed into each project that uses it. It lives either in
  the framework repository's `packages/` (a package the framework's maintainers keep) or in a repository of its
  own (docs/EXTENSION_STANDARD.md, "Where a package lives").

Write the answer and its reason in the extension's module docstring (local) or its README (package). It decides
everything below.

## 2. Scaffold

- **Project-local:** from the project folder, `supervisor extension new <name>`. It writes `extensions/<name>.py`
  and `extensions/<name>.toml` (the manifest) and lists the name in `project.yaml`.
- **Package:** `supervisor extension new <name> --package --dir <where>` (in the framework repository,
  `--dir packages`). It writes the folder `supervisor-ext-<name>` (with `_` as `-`). That folder holds:
  - `pyproject.toml`, with the entry point in the `supervisor.extensions` group;
  - the package `supervisor_ext_<name>`, with its manifest and a pydantic config model;
  - a generated conformance test and a behaviour test;
  - a README and a CHANGELOG;
  - `[tool.supervisor.ci]` in `pyproject.toml`: what the framework repository's CI drives on the installed wheel
    (its python job; name a `lane`, or a `config` table when a field is required, as it changes).

`<name>` is the extension's **identity**: lower case letters, digits and `_`, starting with a letter. It never
changes once anything is saved under it.

## 3. Fill in the standard

- **`setup(registry)`:** registers everything, and only there. Nothing may register at import.
- **The manifest's `provides`:** declares exactly what `setup()` registers.
  - Every identifier starts with the identity (`<identity>`, `<identity>.<x>`, `<identity>_<x>`). The scaffold's
    commented examples already do.
  - An older name that predates the rule goes in `legacy_ids`.
- **`framework`:** the range of framework versions it supports. The scaffold writes `>=<this major.minor>,<next
  major>` (for example `>=1.12,<2`). Its lower end is the oldest version it is tested against.
- **`config`:** the section `extensions.<identity>` and its schema, `module:Class` (a pydantic model). A
  project-local extension names its own module (`<name>:Settings`); a package names its config module
  (`supervisor_ext_<name>.config:Settings`). Read the section with `ext.config(layout, "<identity>")`. The local
  scaffold writes neither the `[config]` table nor a `Settings` class (the package scaffold writes both): add them
  when the extension has configuration.
- **`requires`:**
  - the resources it reads (`gpu`, through `ext.resources()`);
  - the extensions it calls into, which load first;
  - the external services it needs.
- **Imports:** `supervisor.ext` only (and `supervisor.testing` in its tests). Never an internal module.
- **Doctor checks:** a doctor check reports a name its manifest lists in `provides.doctor_checks`.

## 4. Test it

- **The behaviour tests:** what the extension does, on the fake lanes (`supervisor.testing`).
  - A project-local extension's tests live in the project's `tests/`. Build a throwaway project with
    `testing.project(tmp_path, modules={"<name>": <source>}, files={"extensions/<name>.toml": <manifest>},
    extensions=["<name>"])`: pass the manifest through `files=`, since `modules=` writes only the module.
  - A package's tests live in its own `tests/`. Install the package into the test environment first
    (`pip install -e <package folder>`), so its entry point is found. Offline, add `--no-build-isolation`; the
    environment then needs `setuptools` (at the version the package's `pyproject.toml` asks) and `wheel` installed.
  - `supervisor.testing` drives tasks but does not read a task's status back: a test reads
    `state/tasks/<id>.yaml` itself (it is YAML, with a `status` field). Doctor checks run only through a
    `supervisor doctor` subprocess.
- **`supervisor extension check <name>` (gate G5):** every check OK.
  - It reads the manifest without import, and checks the framework range.
  - It checks the imports, and that importing changes no registry (in a fresh process).
  - It sets `setup()` up twice against a staging registry, and validates the configuration.
- **`supervisor doctor`:** the extension loads, and each doctor check reports a name its manifest declares. G5
  does not run doctor checks, so only doctor catches a wrong name there.

## Checks it ends with

- `supervisor extension check <name>` (G5): all checks OK.
- `supervisor doctor` (G6): the extension loaded, its doctor checks named as declared.
- The extension's behaviour suite: for a package, against its declared framework range (oldest and newest version).
- No framework file changed. If the extension needs a new hook or API, stop: that is the framework-change skill
  first.
