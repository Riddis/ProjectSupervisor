# Plugin packages

Plugin packages the framework's maintainers keep (docs/PLUGIN_STANDARD.md, "Where a package lives"). Each
folder here is one distribution, `packages/supervisor-plugin-<name>/`, with its own `pyproject.toml`, version,
CHANGELOG and tests. None of them is part of the framework's wheel: a project installs a package's wheel when it
wants that package.

- **Create one** with `supervisor plugin new <identity> --package --dir packages` (the plugin-new skill), or
  promote a project's plugin (the plugin-promote skill). Examples and defaults use invented names: no
  project's information, here or in the wheel.
- **CI** runs a job per package against the oldest framework release in its manifest's range and against HEAD
  (`tools/ci_package.py`). Name what that job drives in the package's `pyproject.toml`:

  ```toml
  [tool.supervisor.ci]
  lane = "<adapter>"            # or python_job = "<name>"
  [tool.supervisor.ci.config]   # its plugins.<identity> section, when a field is required
  ```

- **Release** with the plugin-release skill: the tag is `<identity>-vX.Y.Z`, never `vX.Y.Z` (that is the
  framework's).
