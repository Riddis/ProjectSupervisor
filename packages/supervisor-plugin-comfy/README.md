# supervisor-plugin-comfy

A Supervisor plugin, identity `comfy`. It follows the framework's plugin standard
(`docs/PLUGIN_STANDARD.md` in the framework repo).

- Install it into a project's environment (`pip install -e .` while developing), then list `comfy` under
  `plugins:` in the project's `project.yaml`.
- Configure it under `plugins.comfy` in the project's `config/routing.yaml`.
- `vram_device` (optional): a key of `resources.gpu.devices`, or a bare integer index, naming the card this
  lane's preflight, peak-VRAM record, `check_gpu` and overview panel read. Unset: the single-card behaviour of
  every prior release.
- `supervisor plugin check comfy` (gate G5), run from that project, proves it conforms; `pytest` runs its
  own tests.
