# {{ distribution }}

A Supervisor plugin, identity `{{ identity }}`. It follows the framework's plugin standard
(`docs/PLUGIN_STANDARD.md` in the framework repo).

- Install it into a project's environment (`pip install -e .` while developing), then list `{{ identity }}` under
  `plugins:` in the project's `project.yaml`.
- Configure it under `plugins.{{ identity }}` in the project's `config/routing.yaml`.
- `supervisor plugin check {{ identity }}` (gate G5), run from that project, proves it conforms; `pytest` runs its
  own tests.
