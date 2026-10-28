---
name: extension-release
description: Release a new version of a Supervisor extension package - version and changelog, build the wheel, test the installed artifact (not the source tree) against the oldest and newest framework in its range, verify what the wheel carries, and identify the exact artifact. Use when the user says "release the extension", "publish a new version of the package", "cut <package> x.y.z", "/extension-release".
---

# Releasing an extension package

A project installs the wheel, not the repository, so the wheel is what gets tested. A release names one exact
artifact (version and hash) that was tested, and nothing else.

## 1. Version and changelog

- The version rule is the framework's (`CHANGELOG.md` at the framework root):
  - **patch:** a fix;
  - **minor:** a feature, or a new identifier;
  - **major:** a change a project must act on, such as a renamed identifier, a moved config section or a dropped
    framework version.
- The package's CHANGELOG gains the section, with **Migration** (what `supervisor migrate` or the package's
  migrations change in a project) and **Rollback** (how to go back: the previous version plus a snapshot restore,
  or a tested reverse migration).
- The manifest's `version` equals the package version. Its `framework` range says which framework versions this
  release supports.

## 2. Build, then test what was built

1. Build the wheel from a clean checkout at the release commit (`python -m build`, or `pip wheel --no-deps`).
2. Check the wheel carries everything the extension needs at run time:
   - the package;
   - `supervisor-extension.toml`;
   - its config schema's module;
   - its migrations module;
   - any templates or static files it serves.
   List the wheel's contents and compare.
3. In a **fresh environment**, with the source checkout out of reach (moved, or not on `sys.path`):
   - install the **oldest** framework release in the range, then the wheel;
   - in a disposable project listing the identity, the extension is found through its entry point, loads, passes
     `supervisor extension check <identity>` (G5), and completes a task on its lane if it has one.
     `tools/ci_installed_extension.py` in the framework repository does exactly this.
   - run the package's own suite against the installed wheel.
4. Repeat step 3 with the **newest** framework release in the range.

## 3. Identify the artifact

Record, for the release:
- the version;
- the wheel's file name and its sha256;
- the source commit;
- the framework versions it was tested against, with the results.

Tag the source commit (`vX.Y.Z`).

## 4. The catalog

A package listed in the framework's catalog gets its index entry updated in a pull request to the framework, with
the version, the artifact hash and the date of the G5 run. The maintainers re-run G5 on that exact artifact before
listing it. The catalog arrives with phase 6 of the boundary plan; until then, record the artifact in the package's
own release notes.

## Checks it ends with

- G5 on the installed wheel, against the oldest and the newest framework in the range.
- The package's suite on the installed wheel, against both.
- The artifact identified: version, wheel name, sha256, commit and tag.
