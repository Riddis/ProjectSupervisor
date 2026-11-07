# Changelog

## 1.1.0

- `vram_device` (optional, declared): names the card this lane's preflight, peak-VRAM record, `check_gpu` and
  `gpu_panel` read - a key of `resources.gpu.devices`, or a bare integer index - resolved through the
  framework's `gpu_device` (2.3.0). A project that configures a device also gets that device's own floor by
  default (falling back to `resources.gpu.usable_vram_floor_mb` as before), and the `CapacityDeferred` message
  names the device. Unset (the default): byte-identical to 1.0.0's argv, floor, message, doctor line and panel.
- `framework` raised to `>=2.3,<3`: the resolver and the device-aware probe do not exist before 2.3.0.
- `check_gpu` catches a misconfigured `vram_device` and reports it as a `FAIL` under its own name, rather than
  letting the exception crash the check.

## 1.0.0

- Adapter `comfy`, generating against a ComfyUI server over its HTTP API.
- Doctor checks for the ComfyUI install, the API, ffmpeg and the GPU; pinned custom nodes checked by git SHA
  against a lock file.
- A GPU overview panel hook.

## 0.1.0

- Scaffolded by `supervisor plugin new comfy --package` (framework 2.0.0).
