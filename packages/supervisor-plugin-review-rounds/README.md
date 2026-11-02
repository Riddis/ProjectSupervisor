# supervisor-plugin-review-rounds

A Supervisor plugin, identity `review_rounds`. It follows the framework's plugin standard
(`docs/PLUGIN_STANDARD.md` in the framework repo).

- Install it into a project's environment (`pip install -e .` while developing), then list `review_rounds` under
  `plugins:` in the project's `project.yaml`.
- Configure it under `plugins.review_rounds` in the project's `config/routing.yaml`.
- `supervisor plugin check review_rounds` (gate G5), run from that project, proves it conforms; `pytest` runs its
  own tests.
