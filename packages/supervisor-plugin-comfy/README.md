# supervisor-plugin-comfy

A Supervisor plugin, identity `comfy`. It follows the framework's plugin standard
(`docs/PLUGIN_STANDARD.md` in the framework repo).

- Install it into a project's environment (`pip install -e .` while developing), then list `comfy` under
  `plugins:` in the project's `project.yaml`.
- Configure it under `plugins.comfy` in the project's `config/routing.yaml`.
- `supervisor plugin check comfy` (gate G5), run from that project, proves it conforms; `pytest` runs its
  own tests.
