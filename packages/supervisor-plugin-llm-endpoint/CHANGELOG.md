# Changelog

## 1.0.0

- Adapter `llm_endpoint`: a generic OpenAI-compatible chat-completions lane, streaming, with an optional
  schema the adapter re-validates itself, a VRAM preflight across every configured device, `gpu = True`, and
  `CapacityDeferred` when the lane is not available now (VRAM below floor, or the machine-wide GPU lease held
  by another project).
- A server watchdog (`_server.py`): starts the configured `argv` as its own child, polls the configured health
  URL until ready or `start_timeout_s`, holds the machine-wide GPU lease for as long as it runs, and stops
  (releasing the lease) on idle (`idle_stop_s`), on a release request from another project, or on the owner
  command `llm_endpoint.stop`.
- A doctor check: the endpoint reachable, the configured model present, the watchdog's state file consistent
  with the process table.
- Owner commands `llm_endpoint.start` and `llm_endpoint.stop`.
