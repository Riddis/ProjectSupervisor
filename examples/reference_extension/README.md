# The reference extension

`reference/` is a project extension that uses every registry hook and every operation of the public extension API
(`supervisor.ext`, `docs/EXTENSION_API.md`), and imports nothing else from `supervisor`. It is an example and a test
fixture, not framework code: it is not shipped in the `supervisor` package, and `tests/test_hook_contracts.py` loads
it as a project would to check each hook's contract.

To try it in a project: copy `reference/` to the project's `extensions/reference/`, add `reference` under
`extensions:` in `project.yaml`, and optionally a `reference:` section in `config/routing.yaml` (see the package
docstring). Its page is at `/reference` in the Control Center.
