# Changelog

## 1.1.0

- `vram_device` (optional, declared): names the card this lane's preflight reads - a key of
  `resources.gpu.devices`, or a bare integer index - resolved through the framework's `gpu_device` (2.3.0). A
  project that configures a device also gets that device's own floor by default (falling back to
  `resources.gpu.usable_vram_floor_mb` as before), and the `CapacityDeferred` message names the device. Unset
  (the default): byte-identical to 1.0.0's argv, floor and message.
- `framework` raised to `>=2.3,<3`: the resolver and the device-aware probe do not exist before 2.3.0.
- `check_ollama` catches a misconfigured `vram_device` and reports it as a `FAIL` under its own name, rather than
  letting the exception crash the check.

## 1.0.0

- Adapter `local_llm`, generating against an Ollama server over its HTTP API, backend-generic with no default
  model.
- A doctor check for the server and the configured model; the optional model-store drive check runs only when
  a drive is configured.

## 0.1.0

- Scaffolded by `supervisor plugin new local_llm --package` (framework 2.0.0).
