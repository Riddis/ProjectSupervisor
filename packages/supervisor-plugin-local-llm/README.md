# supervisor-plugin-local-llm

A Supervisor plugin, identity `local_llm`. It follows the framework's plugin standard
(`docs/PLUGIN_STANDARD.md` in the framework repo).

- Install it into a project's environment (`pip install -e .` while developing), then list `local_llm` under
  `plugins:` in the project's `project.yaml`.
- Configure it under `plugins.local_llm` in the project's `config/routing.yaml`.
- `supervisor plugin check local_llm` (gate G5), run from that project, proves it conforms; `pytest` runs its
  own tests.
