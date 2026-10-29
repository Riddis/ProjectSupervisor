---
name: extension-release
description: Release a new version of a Supervisor extension package - version and changelog, build the wheel, test the installed artifact (not the source tree) against the oldest and newest framework in its range, verify what the wheel carries, and identify the exact artifact. Use when the user says "release the extension", "publish a new version of the package", "cut <package> x.y.z", "/extension-release".
---

# Releasing an extension package

A project installs the wheel, not the repository, so the wheel is what gets tested. A release names one exact
artifact (version and hash) that was tested, and nothing else.

## 1. Version and changelog

- The version follows semantic versioning:
  - **patch:** a fix;
  - **minor:** a feature, or a new identifier;
  - **major:** a change a project must act on, such as a renamed identifier, a moved config section or a dropped
    framework version.
- The first release is `1.0.0` once the package's identifiers and config are meant to stay. Until then, use
  `0.x`, where any release may still change them.
- The package's CHANGELOG gains the section, with **Migration** (what `supervisor migrate` or the package's
  migrations change in a project; "none" when nothing does) and **Rollback** (how to go back: the previous version
  plus a snapshot restore, or a tested reverse migration).
- The manifest's `version` equals the package version. Its `framework` range says which framework versions this
  release supports.

## 2. Build, then test what was built

1. **Build** from a clean checkout of the release commit, so build output never lands in the repository: a fresh
   `git clone` or `git worktree add` at that commit. Then build:
   `python -m pip wheel --no-deps --no-build-isolation -w <out> .`. That works offline, with setuptools and wheel
   already installed in the build environment, at the version `pyproject.toml` requires.
2. **Check the wheel carries everything** the extension needs at run time. List its contents
   (`python -m zipfile -l <wheel>`) and compare:
   - the package;
   - `supervisor-extension.toml`;
   - its config schema's module;
   - its migrations module, if the manifest declares one;
   - any templates or static files it serves.
3. **Keep that file.** Builds are not byte-for-byte reproducible, so the wheel you test is the release; never
   rebuild it afterwards.
4. **In a fresh environment**, with the source checkout out of reach (not on `sys.path`, not the working folder):
   - Install the **oldest** framework release in the range. A framework release is on no package index, so build
     its wheel from its tag the same way (a `git worktree add` at the tag, then `pip wheel --no-deps
     --no-build-isolation`). Install it, then the extension's wheel (`pip install --no-deps`, into a fresh venv or a
     target folder on `PYTHONPATH`).
   - In a disposable project listing the identity, the extension must:
     - be found through its entry point;
     - load;
     - pass `supervisor extension check <identity>` (G5);
     - complete a task on its lane, if it has one.

     The framework repository's `tools/ci_installed_extension.py <identity> [--lane <adapter>]` does these steps.
     For an extension without a lane, check its python jobs or commands by hand.
   - Run the package's own suite against the installed wheel: copy its `tests/` folder to an empty place and run it
     there, so the source cannot be imported.
5. Repeat step 4 with the **newest** framework release in the range.

## 3. Identify the artifact

Record, for the release:
- the version;
- the wheel's file name and its sha256;
- the source commit;
- the framework versions it was tested against, with the results.

Then tag the source commit with that record in the tag's message (`git tag -a vX.Y.Z -m "<the record>"`). The hash
exists only once the wheel is built, so the tag comes after the build, never before.

## 4. Into a project, and the catalog

- A project takes the release in a maintenance window: its project-upgrade skill, with the package in place of the
  framework. Install the tested wheel, pin it with its hash in the project's lock, then migrate, doctor and the
  suite.
- A package listed in the framework's catalog gets its index entry updated in a pull request to the framework:
  the version, the artifact hash and the date of the G5 run. The maintainers re-run G5 on that exact artifact
  before listing it. The catalog arrives with phase 6 of the boundary plan; until then, the tag's record is the
  release record.

## Checks it ends with

- G5 on the installed wheel, against the oldest and the newest framework in the range.
- The package's suite on the installed wheel, against both.
- The artifact identified: version, wheel name, sha256, commit and tag.
