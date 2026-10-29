# The reference plugin

`reference/` is a project plugin that uses every registry hook and every operation of the public plugin API
(`supervisor.plugin`, `docs/PLUGIN_API.md`), and imports nothing else from `supervisor`. It is an example and a test
fixture, not framework code: it is not shipped in the `supervisor` package, and `tests/test_hook_contracts.py` loads
it as a project would to check each hook's contract.

It follows the plugin standard (`docs/PLUGIN_STANDARD.md`):
- `reference/supervisor-plugin.toml` declares everything `setup()` registers.
- `reference/config.py` is the schema of its `plugins.reference` section.
- This folder's `pyproject.toml` builds it as the package `supervisor-plugin-reference`, entry point `reference`.

`supervisor plugin check reference` (gate G5) proves it conforms. CI installs it as a wheel and as an editable
install into a fresh environment (job "G5 reference plugin").

To try it in a project, do either of these, then list `reference` under `plugins:` in `project.yaml`:
- `pip install examples/reference_plugin` into the project's environment;
- copy `reference/` to the project's `plugins/reference/`.

Optionally add a `plugins.reference` section to `config/routing.yaml` (see the package docstring). Its page is
at `/reference` in the Control Center.
