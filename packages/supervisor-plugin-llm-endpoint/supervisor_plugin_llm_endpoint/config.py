"""The configuration this plugin owns: `plugins.llm_endpoint` in a project's `config/routing.yaml`, validated by
this model before `setup()` runs. Read it with `supervisor.plugin.config(layout, "llm_endpoint")`.

Every key is a neutral default or none at all (D-013 design section 6): no model, no weight path, no server
argv assumes a project. `base_url` defaults to `llama.cpp`'s own documented loopback port (the tool's choice,
not a project's); a project that runs a different endpoint, or more than one profile, overrides it entirely.
`argv` has no default: the server command line (binary path, model path, tensor split, `--n-cpu-moe`, context
length, ...) is a project's own measured values (T5-02), never guessed here. Unlike the framework's single-card
GPU-locked lanes, this one has no `vram_device` selector: the design's whole point is a model split by layer
across every configured card, so the adapter's preflight (`resources.gpu.devices`) checks every one of them
against its own floor, not a single chosen one.

`chars_per_token` is the context-budget estimate's own assumption (D-013 section 2.1): a deterministic
characters-per-token ratio, never a call to a model. 3 is conservative for code, diffs, YAML and non-English
text, which tokenise at roughly 2.5-3.5 chars/token with every tokenizer this lane will meet (RELEASE_REVIEW_
local-lane.md F7); a project serving plain English prose may raise it.

`defer_budget_s` is D-013 section 5's R5 row: after this many continuous seconds of deferral the adapter raises
one idempotent owner inbox item naming what the adapter last gave as the reason, and keeps deferring - "Silence
is not a report". 7200 (two hours) is the design's own default; a project may lower it for a faster heads-up.
A float, not an int: the arithmetic it is compared against (`time.time()` deltas) is already float, and a test
wants a sub-second budget to prove the behaviour without a real wait.

`hand_over_grace_s` is R4's fairness clause, the watchdog's own half (RELEASE_REVIEW_local-lane_round2.md F18):
"this lane's watchdog finishes the in-flight request, stops the server and releases"
(LOCAL_MODEL_LANE.md:267-270). The activity file is the in-flight signal - the worker touches it on every
streamed piece - so a release request is honoured only once that file has been quiet for this long; 3.0
seconds is enough margin over the worker's own touch cadence without noticeably delaying a hand-over to an
idle server. Bounded by the lease's own `hand_over_timeout_s` (`resources.gpu.lease`), so a wedged generation
cannot hold the cards past the window either - a float for the same reason `defer_budget_s` is one."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")
    base_url: str = "http://127.0.0.1:8080"
    model: str | None = None
    argv: list[str] | None = None
    health_path: str = "/health"
    start_timeout_s: int = 300
    idle_stop_s: int = 900
    timeout_s: int = 1800
    context_tokens: int = 8192
    max_tokens: int = 2048
    chars_per_token: int = 3
    defer_budget_s: float = 7200.0
    hand_over_grace_s: float = 3.0
