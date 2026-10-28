---
name: extension-new
description: Write a new Supervisor extension - decide whether it is project-local or a package, scaffold it in the standard shape, give it a manifest, identity, config schema and identifiers, and prove it with gate G5. Use when the user says "write an extension", "add a lane/command/page/check as an extension", "new extension package", "/extension-new".
---

# Writing an extension

An extension is code the framework loads into a project through `supervisor.ext`, its only public API. Its shape is
`docs/EXTENSION_STANDARD.md`; its API is `docs/EXTENSION_API.md`. A change every project would want is not an
extension but a framework change (the framework-change skill).

## 1. Local or package (rule 1)

Ask: would a different project want this, as it is?
- **No:** it is **project-local**, a module in the project's `extensions/` folder.
- **Yes, or it will be shared:** it is a **package** in its own repository, installed into each project that uses
  it.

Write the answer down with its reason; it decides everything below.

## 2. Scaffold

- **Project-local:** from the project folder, `supervisor extension new <name>`. It writes `extensions/<name>.py`
  and `extensions/<name>.toml` (the manifest) and lists the name in `project.yaml`.
- **Package:** `supervisor extension new <name> --package --dir <where>`. This writes the standard package:
  - `pyproject.toml`, with the entry point in the `supervisor.extensions` group;
  - the package itself, with its manifest and a pydantic config model;
  - a generated conformance test and a behaviour test;
  - a README and a CHANGELOG.

`<name>` is the extension's **identity**: lower case letters, digits and `_`, starting with a letter. It never
changes once anything is saved under it.

## 3. Fill in the standard

- **`setup(registry)`:** registers everything, and only there. Nothing may register at import.
- **The manifest's `provides`:** declares exactly what `setup()` registers.
  - Every identifier starts with the identity (`<identity>`, `<identity>.<x>`, `<identity>_<x>`).
  - An older name that predates the rule goes in `legacy_ids`.
- **`framework`:** the range of framework versions it supports (`>=1.13,<2`). Its lower end is the oldest version
  it is tested against.
- **`config`:** the section `extensions.<identity>` and its schema (`module:Class`, a pydantic model). Read it with
  `ext.config(layout, "<identity>")`.
- **`requires`:**
  - the resources it reads (`gpu`, through `ext.resources()`);
  - the extensions it calls into, which load first;
  - the external services it needs.
- **Imports:** `supervisor.ext` only (and `supervisor.testing` in its tests). Never an internal module.
- **Doctor checks:** a doctor check reports a name its manifest lists in `provides.doctor_checks`.

## 4. Test it

- The behaviour tests, on the fake lanes (`supervisor.testing`): what the extension does, not how.
- `supervisor extension check <name>` (gate G5): every check OK.
  - It reads the manifest without import, and checks the framework range.
  - It checks the imports, and that importing changes no registry (in a fresh process).
  - It sets `setup()` up twice against a staging registry, and validates the configuration.
- For a package, the generated conformance test and the behaviour test pass in its own environment.

## Checks it ends with

- `supervisor extension check <name>` (G5): all checks OK.
- The extension's behaviour suite, across its declared framework range for a package (its oldest and newest
  version).
- No framework file changed. If the extension needs a new hook or API, stop: that is the framework-change skill
  first.
