# The reference extension

`reference/` is a project extension that uses every registry hook and every operation of the public extension API
(`supervisor.ext`, `docs/EXTENSION_API.md`), and imports nothing else from `supervisor`. It is an example and a test
fixture, not framework code: it is not shipped in the `supervisor` package, and `tests/test_hook_contracts.py` loads
it as a project would to check each hook's contract.

It follows the extension standard (`docs/EXTENSION_STANDARD.md`):
- `reference/supervisor-extension.toml` declares everything `setup()` registers.
- `reference/config.py` is the schema of its `extensions.reference` section.
- This folder's `pyproject.toml` builds it as the package `supervisor-ext-reference`, entry point `reference`.

`supervisor extension check reference` (gate G5) proves it conforms. CI installs it as a wheel and as an editable
install into a fresh environment (job "G5 reference extension").

To try it in a project, do either of these, then list `reference` under `extensions:` in `project.yaml`:
- `pip install examples/reference_extension` into the project's environment;
- copy `reference/` to the project's `extensions/reference/`.

Optionally add an `extensions.reference` section to `config/routing.yaml` (see the package docstring). Its page is
at `/reference` in the Control Center.
