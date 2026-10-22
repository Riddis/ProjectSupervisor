"""Claude Messages API adapter: a single bounded HTTP
call to api.anthropic.com for unattended runs, so overnight batches draw a metered API credit allowance
(`routing.yaml claude_api.monthly_budget_usd`) instead of the owner's interactive subscription window
(routing.yaml: `capacity.unattended_prefers_api_credits`, the `scheduler.plan(unattended=True)` switch).

No tool use: the Messages API call carries exactly the same role prompt text `claude_cli` puts on stdin
(`packets.delivery_brief`) as a single user message, and the same fenced-JSON result contract is expected back
(`structured_result`). The model cannot Read/Write/Edit or run Bash through this adapter — it is for
reasoning/text roles (research, review, planning) run unattended, not file-writing engineering work. Disabled
(a clear FAILED result, never raised) while `secrets/.env` carries no `ANTHROPIC_API_KEY` — see
docs/runbooks/claude_api.md and secrets/README.md for how the owner places the key and approves a budget.

Runs synchronously inside submit() like the `python` adapter (one bounded call, not a long-lived child); the
finished result lives in `result.json` before submit() returns, so a crash between submit and the loop reading
collect() never re-spends the call."""
from __future__ import annotations

import json
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path

import requests

from .. import policy
from .._vendor.contract import atomic_json, structured_result
from ..paths import Layout
from ..store import now_iso
from .base import Adapter, Result, register
from .claude_cli import classify_status, job_spec, role_profile, routing

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT_S = 600
DEFAULT_BUDGET_USD = 100.0           # fallback when routing.yaml claude_api.monthly_budget_usd is absent (the template sets 0 = lane off)
LEDGER_CATEGORY = "api_credits"
# Model ids the Messages API accepts; routing.yaml roles.<role>.model uses the CLI's short aliases.
MODEL_ALIASES = {"sonnet": "claude-sonnet-5", "opus": "claude-opus-5", "haiku": "claude-haiku-4-5-20251001"}


def model_id(name: str) -> str:
    return MODEL_ALIASES.get(name, name)


# ------------------------------------------------------------------ secrets/.env and config

def _env_file(layout: Layout) -> Path:
    return layout.secrets / ".env"


def read_dotenv(path: Path) -> dict:
    """Minimal `KEY=VALUE` parser (blank/`#` lines skipped, one optional matching pair of quotes stripped);
    never referenced from packets, never logged (secrets/README.md)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def api_key(layout: Layout) -> str | None:
    return read_dotenv(_env_file(layout)).get("ANTHROPIC_API_KEY") or None


def api_config(layout: Layout) -> dict:
    cfg = dict(routing(layout).get("claude_api") or {})
    cfg.setdefault("monthly_budget_usd", DEFAULT_BUDGET_USD)
    cfg.setdefault("pricing_usd_per_mtok", {})
    cfg.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
    cfg.setdefault("timeout_s", DEFAULT_TIMEOUT_S)
    return cfg


# ------------------------------------------------------------------ cost ledger (state/runtime/cost_ledger.jsonl)

def ledger_path(layout: Layout) -> Path:
    return layout.runtime / "cost_ledger.jsonl"


def _price(cfg: dict, model: str, kind: str) -> float:
    return float((cfg.get("pricing_usd_per_mtok") or {}).get(model, {}).get(kind, 0.0))


def record_usage(layout: Layout, task_id: str, attempt_id: str, model: str, usage: dict) -> dict:
    """Append one ledger entry for a completed call (plain JSONL append, same durability as `tasks.log`; a torn
    last line is skipped by readers). Cost is 0 for a model with no configured rate — never blocks on a missing price."""
    cfg = api_config(layout)
    input_tok, output_tok = int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
    cost = input_tok / 1_000_000 * _price(cfg, model, "input") + output_tok / 1_000_000 * _price(cfg, model, "output")
    entry = {"ts": now_iso(), "category": LEDGER_CATEGORY, "task_id": task_id, "attempt_id": attempt_id,
             "model": model, "input_tokens": input_tok, "output_tokens": output_tok, "cost_usd": round(cost, 6)}
    path = ledger_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _month_key(ts: str | None = None) -> str:
    dt = datetime.fromisoformat(ts) if ts else datetime.now(UTC)
    return f"{dt.year:04d}-{dt.month:02d}"


def spent_this_month(layout: Layout) -> float:
    path = ledger_path(layout)
    if not path.exists():
        return 0.0
    now_key = _month_key()
    total = 0.0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("category") == LEDGER_CATEGORY and _month_key(entry.get("ts")) == now_key:
            total += float(entry.get("cost_usd") or 0.0)
    return total


def available(layout: Layout) -> bool:
    """False while the owner has not placed a key in secrets/.env: the adapter
    is disabled, not gated-and-waiting, since no amount of time fixes a missing key."""
    return bool(api_key(layout))


def credits_remaining(layout: Layout) -> bool:
    return available(layout) and spent_this_month(layout) < api_config(layout)["monthly_budget_usd"]


# ------------------------------------------------------------------ adapter

class ClaudeApiAdapter(Adapter):
    name = "claude_api"

    def _post(self, url: str, headers: dict, payload: dict, timeout: float):
        return requests.post(url, headers=headers, json=payload, timeout=timeout)

    def prepare(self, layout: Layout, task, attempt_id: str) -> dict:
        from .. import packets
        job = job_spec(task)
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        role = job.get("role") or task.owner_role
        prof = role_profile(layout, role)
        prof.update({k: v for k, v in job.items() if k in ("model", "effort")})
        cfg = api_config(layout)
        prompt_text = policy.assert_packet_clean(job.get("prompt") or packets.delivery_brief(layout, task),
                                                layout, f"{self.name} request {task.id}/{attempt_id}")
        request = {"model": model_id(prof["model"]), "max_tokens": int(job.get("max_tokens", cfg["max_tokens"])),
                   "messages": [{"role": "user", "content": prompt_text}]}
        atomic_json(run_dir / "request.json", request)
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
                "root": str(layout.root), "result_kind": job.get("result_kind", "delivery"),
                "timeout_s": int(job.get("timeout_s", cfg["timeout_s"]))}

    def submit(self, spec: dict) -> dict:
        run_dir = Path(spec["run_dir"])
        result_path = run_dir / "result.json"
        if result_path.exists():          # crash between submit and the loop collecting it: never re-spend the call
            return {"kind": "claude_api", "result_path": str(result_path)}
        layout = Layout(spec["root"])
        key = api_key(layout)
        if not key:
            atomic_json(result_path, {"status": "FAILED", "error": "claude_api disabled: no ANTHROPIC_API_KEY in "
                        "secrets/.env (docs/runbooks/claude_api.md)"})
            return {"kind": "claude_api", "result_path": str(result_path)}
        if not credits_remaining(layout):
            atomic_json(result_path, {"status": "FAILED",
                        "error": "claude_api disabled: monthly API credit budget exhausted"})
            return {"kind": "claude_api", "result_path": str(result_path)}
        request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
        headers = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        try:
            resp = self._post(API_URL, headers, request, spec.get("timeout_s", DEFAULT_TIMEOUT_S))
        except requests.RequestException as exc:
            atomic_json(result_path, {"status": "FAILED", "error": f"claude_api request failed: {exc}"})
            return {"kind": "claude_api", "result_path": str(result_path)}
        if resp.status_code >= 400:
            atomic_json(result_path, {"status": "FAILED", "error": f"claude_api http {resp.status_code}: {resp.text[:500]}"})
            return {"kind": "claude_api", "result_path": str(result_path)}
        body = resp.json()
        atomic_json(run_dir / "response.json", body)
        usage = body.get("usage") or {}
        record_usage(layout, spec["task_id"], spec["attempt_id"], request["model"], usage)
        text = "".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
        try:
            obj = structured_result(text, spec.get("result_kind", "delivery"))
        except ValueError:
            atomic_json(result_path, {"status": "FAILED", "error": "no structured result in claude_api response",
                        "usage": usage, "model": request["model"]})
            return {"kind": "claude_api", "result_path": str(result_path)}
        status = classify_status(obj)
        notes = str(obj.get("notes") or obj.get("summary") or "")
        atomic_json(result_path, {"status": status, "artifacts": list(obj.get("artifacts") or []), "notes": notes,
                    "usage": usage, "model": request["model"], "raw": obj})
        return {"kind": "claude_api", "result_path": str(result_path)}

    def recover(self, spec: dict) -> dict | None:
        p = Path(spec["run_dir"]) / "result.json"
        return {"kind": "claude_api", "result_path": str(p)} if p.exists() else None

    def alive(self, handle: dict) -> bool:
        return False

    def collect(self, spec: dict, handle: dict) -> Result | None:
        p = Path(handle["result_path"])
        if not p.exists():
            return Result(status="FAILED", error="claude_api left no result")
        payload = json.loads(p.read_text(encoding="utf-8"))
        return Result(status=payload.get("status", "FAILED"), artifacts=payload.get("artifacts", []),
                      notes=payload.get("notes", ""), error=payload.get("error"), model=payload.get("model"),
                      usage=payload.get("usage", {}), raw=payload)


register(ClaudeApiAdapter())
