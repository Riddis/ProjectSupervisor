# {{ distribution }}

A Supervisor extension, identity `{{ identity }}`. It follows the framework's extension standard
(`docs/EXTENSION_STANDARD.md` in the framework repo).

- Install it into a project's environment (`pip install -e .` while developing), then list `{{ identity }}` under
  `extensions:` in the project's `project.yaml`.
- Configure it under `extensions.{{ identity }}` in the project's `config/routing.yaml`.
- `supervisor extension check {{ identity }}` (gate G5), run from that project, proves it conforms; `pytest` runs its
  own tests.
