# supervisor-plugin-llm-endpoint

A Supervisor plugin, identity `llm_endpoint`. It follows the framework's plugin standard
(`docs/PLUGIN_STANDARD.md` in the framework repo).

A generic OpenAI-compatible chat-completions lane for text-only work: text in, text or schema-validated JSON
out. It knows nothing about any particular server — the binary, its argv, the health URL and the model name are
all configuration — so the same lane serves `llama.cpp`'s `llama-server`, Ollama's OpenAI-compatible endpoint,
or anything else that speaks the shape.

- Install it into a project's environment (`pip install -e .` while developing), then list `llm_endpoint` under
  `plugins:` in the project's `project.yaml`.
- Configure it under `plugins.llm_endpoint` in the project's `config/routing.yaml`: `base_url`, `model`, `argv`
  (the server's own command line, started and stopped by this package's watchdog), `health_path`,
  `start_timeout_s`, `idle_stop_s`, `timeout_s`, `context_tokens`, `max_tokens`, `chars_per_token` (the
  context-budget estimate's own assumption; 3 by default, conservative for code and non-English text),
  `defer_budget_s` (seconds of continuous deferral before the adapter raises one idempotent owner inbox item
  naming the reason it keeps deferring - "Silence is not a report"; 7200, two hours, by default, and a project
  may lower it for an earlier heads-up), `hand_over_grace_s` (seconds the in-flight activity file must sit
  quiet before the watchdog honours another project's release request, so a request mid-generation is
  finished rather than cut; 3.0 by default, bounded by `resources.gpu.lease.hand_over_timeout_s` so a wedged
  generation still cannot hold the cards past that window).
- `profile`: a project may configure one `profile` under `plugins.llm_endpoint` (default `"default"`); a task
  that requests any other profile is refused rather than served by it (no multi-profile switching yet).
- `gpu = True`: this lane holds the project's single resource slot (`limits.max_resource_jobs`) and, when the
  project configures `resources.gpu.lease`, the machine-wide GPU lease for as long as its server is up.
- Owner commands: `llm_endpoint.start` and `llm_endpoint.stop` start or stop the managed server by hand.
- `supervisor plugin check llm_endpoint` (gate G5), run from that project, proves it conforms; `pytest` runs its
  own tests (a fake HTTP server stands in for the real one; none of this package's tests starts a real model
  server or reaches a GPU).
