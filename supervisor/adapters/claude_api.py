"""Claude Messages API adapter: a single bounded HTTP
call to api.anthropic.com for unattended runs, so overnight batches draw a metered API credit allowance
(`routing.yaml claude_api.monthly_budget_usd`) instead of the owner's interactive subscription window
(routing.yaml: `capacity.unattended_prefers_api_credits`, the `scheduler.plan(unattended=True)` switch).

No tool use: the Messages API call carries exactly the same role prompt text `claude_cli` puts on stdin
(`packets.delivery_brief`) as a single user message, and the same fenced-JSON result contract is expected back
(`structured_result`). The model cannot Read/Write/Edit or run Bash through this adapter — it is for
reasoning/text roles (research, review, planning) run unattended, not file-writing engineering work, which is
why the unattended switch only moves a task here when `unattended_eligible()` says its contract needs no tools.
Disabled
(a clear FAILED result, never raised) while `secrets/.env` carries no `ANTHROPIC_API_KEY` — see
docs/runbooks/claude_api.md and secrets/README.md for how the owner places the key and approves a budget.
A spent prepaid balance is not a failure: the Console refuses the request without billing it, submit() raises
LaneUnavailable and marks the lane spent (`state/runtime/claude_api_credit.json`) until a recheck, the scheduler
stops moving work here, and the loop runs a delivery it had moved on the lane it came from.

Runs synchronously inside submit() like the `python` adapter (one bounded call, not a long-lived child). One
attempt makes at most one paid POST: the response body is persisted before anything is derived from it, and a
re-entered submit() finishes from `response.json` (ledger included, idempotently) instead of calling again."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path

import requests

from .. import policy
from .._vendor.contract import LaneUnavailable, atomic_json, read_json, structured_result
from ..paths import Layout
from ..store import now_iso
from .base import Adapter, Result, register
from .claude_cli import classify_status, job_spec, role_profile, routing

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT_S = 600
DEFAULT_BUDGET_USD = 100.0           # fallback when routing.yaml claude_api.monthly_budget_usd is absent (the template sets 0 = lane off)
DEFAULT_RECHECK_HOURS = 24.0         # how long a spent balance keeps the lane off before one request tries again
# The Console's refusal once the org's credit balance is spent (HTTP 400, invalid_request_error); nothing is billed.
CREDIT_REFUSAL = "credit balance is too low"
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
    cfg.setdefault("eligible_roles", [])
    cfg.setdefault("exhausted_recheck_hours", DEFAULT_RECHECK_HOURS)
    return cfg


# ------------------------------------------------------------------ cost ledger (state/runtime/cost_ledger.jsonl)

def ledger_path(layout: Layout) -> Path:
    return layout.runtime / "cost_ledger.jsonl"


def _price(cfg: dict, model: str, kind: str) -> float:
    return float((cfg.get("pricing_usd_per_mtok") or {}).get(model, {}).get(kind, 0.0))


def entries(layout: Layout) -> list[dict]:
    """Every readable ledger line (a torn last line from a kill mid-append is skipped, as for `tasks.log`)."""
    path = ledger_path(layout)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def recorded(layout: Layout, task_id: str, attempt_id: str, response_id: str) -> dict | None:
    """The entry already written for this API response, if any. One attempt makes at most one paid call, so
    (task, attempt, response id) identifies it; a crash between the append and `result.json` makes submit()
    process the same persisted response again, and it must not be billed twice."""
    for entry in entries(layout):
        if (entry.get("category"), entry.get("task_id"), entry.get("attempt_id"), entry.get("response_id") or "") == \
           (LEDGER_CATEGORY, task_id, attempt_id, response_id):
            return entry
    return None


def record_usage(layout: Layout, task_id: str, attempt_id: str, model: str, usage: dict, response_id: str = "") -> dict:
    """Append one ledger entry for a completed call (plain JSONL append, same durability as `tasks.log`). Idempotent
    per response: re-processing a persisted response returns the existing entry instead of appending a second one.
    Cost is 0 for a model with no configured rate — never blocks on a missing price."""
    prior = recorded(layout, task_id, attempt_id, response_id)
    if prior is not None:
        return prior
    cfg = api_config(layout)
    input_tok, output_tok = int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
    cost = input_tok / 1_000_000 * _price(cfg, model, "input") + output_tok / 1_000_000 * _price(cfg, model, "output")
    entry = {"ts": now_iso(), "category": LEDGER_CATEGORY, "task_id": task_id, "attempt_id": attempt_id,
             "model": model, "response_id": response_id, "input_tokens": input_tok, "output_tokens": output_tok,
             "cost_usd": round(cost, 6)}
    path = ledger_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _month_key(ts: str | None = None) -> str:
    dt = datetime.fromisoformat(ts) if ts else datetime.now(UTC)
    return f"{dt.year:04d}-{dt.month:02d}"


def spent_this_month(layout: Layout) -> float:
    now_key = _month_key()
    total = 0.0
    for entry in entries(layout):
        if entry.get("category") == LEDGER_CATEGORY and _month_key(entry.get("ts")) == now_key:
            total += float(entry.get("cost_usd") or 0.0)
    return total


def _next_month_epoch() -> float:
    now = datetime.now(UTC)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=UTC).timestamp()


# ------------------------------------------------------------------ spent prepaid balance (state/runtime/claude_api_credit.json)

def credit_state_path(layout: Layout) -> Path:
    return layout.runtime / "claude_api_credit.json"


def credit_refusal(status_code: int, text: str) -> bool:
    """The Console refused the request because the org's credit balance is spent — not a fault of the job."""
    return 400 <= status_code < 500 and CREDIT_REFUSAL in (text or "").lower()


def mark_credit_exhausted(layout: Layout, message: str) -> float:
    """Record the spent balance; returns the recheck epoch. When credit refreshes is the Console's business (the
    plan's billing cycle, a top-up), so after `exhausted_recheck_hours` one request simply tries again — a refused
    request costs nothing."""
    recheck = time.time() + float(api_config(layout)["exhausted_recheck_hours"]) * 3600
    layout.runtime.mkdir(parents=True, exist_ok=True)
    atomic_json(credit_state_path(layout), {"exhausted_at": now_iso(), "recheck_epoch": recheck,
                                            "message": str(message)[:500]})
    return recheck


def credit_exhausted(layout: Layout) -> bool:
    path = credit_state_path(layout)
    if not path.exists():
        return False
    try:
        return float(read_json(path).get("recheck_epoch") or 0) > time.time()
    except (ValueError, TypeError, OSError):
        return True                      # an unreadable mark keeps the lane off; a successful call removes it


def clear_credit_exhausted(layout: Layout) -> None:
    credit_state_path(layout).unlink(missing_ok=True)


def available(layout: Layout) -> bool:
    """False while the owner has not placed a key in secrets/.env: the adapter
    is disabled, not gated-and-waiting, since no amount of time fixes a missing key."""
    return bool(api_key(layout))


def credits_remaining(layout: Layout) -> bool:
    return (available(layout) and not credit_exhausted(layout)
            and spent_this_month(layout) < api_config(layout)["monthly_budget_usd"])


# ------------------------------------------------------------------ what may be moved onto credits unattended

def unattended_eligible(layout: Layout, task, job: dict) -> bool:
    """May the unattended switch move this `claude_cli` pick onto API credits? Switching on the adapter name
    alone sends engineering tasks to a tool-less lane, where they can neither read their inputs nor write the
    artifacts and run the tests their contract requires. Three conditions, all necessary:

    * the pick is the cost-category default — an explicit `job.adapter` is a deliberate choice and is never
      overridden;
    * the contract needs no tools: no declared `artifacts`, no `tests`, no executable `checks`. There is no
      Write/Edit/Bash through the Messages API, and `loop.ingest` requires test evidence from a model lane for any
      task that declares tests, so such a task could only fail here;
    * it is opted in, per task (`job.api_credits: true`) or per role (`routing.yaml claude_api.eligible_roles`,
      empty by default — the owner/architect adds a role only once its tasks are self-contained text jobs).
    """
    if job.get("adapter"):
        return False
    if list(getattr(task, "artifacts", []) or []) or list(getattr(task, "tests", []) or []) or \
       list(getattr(task, "checks", []) or []):
        return False
    if job.get("api_credits") is True:
        return True
    role = job.get("role") or getattr(task, "owner_role", "")
    return role in (api_config(layout).get("eligible_roles") or [])


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
        prompt_text = packets.worker_prompt(task, job, job.get("prompt") or packets.delivery_brief(layout, task))
        prompt_text = policy.assert_packet_clean(prompt_text, layout, f"{self.name} request {task.id}/{attempt_id}",
                                                job=job, task=task, lane=self.name)   # hosted lane: screens see the job too
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
        saved = run_dir / "response.json"
        if saved.exists():
            # The call was made and its body is durable, but the ledger append or the result write did not finish
            # (a kill in that window). Resume from the saved body: the money is already spent, so POSTing again
            # would spend it twice.
            request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
            return self._finish(spec, layout, request, json.loads(saved.read_text(encoding="utf-8")))
        key = api_key(layout)
        if not key:
            atomic_json(result_path, {"status": "FAILED", "error": "claude_api disabled: no ANTHROPIC_API_KEY in "
                        "secrets/.env (docs/runbooks/claude_api.md)"})
            return {"kind": "claude_api", "result_path": str(result_path)}
        if credit_exhausted(layout):
            recheck = float(read_json(credit_state_path(layout)).get("recheck_epoch") or 0)
            raise LaneUnavailable(recheck, "claude_api: credit balance spent; next try after the recheck")
        if float(api_config(layout)["monthly_budget_usd"]) <= 0:
            atomic_json(result_path, {"status": "FAILED", "error": "claude_api disabled: routing.yaml "
                        "claude_api.monthly_budget_usd is 0"})
            return {"kind": "claude_api", "result_path": str(result_path)}
        if not credits_remaining(layout):
            raise LaneUnavailable(_next_month_epoch(), "claude_api: monthly budget (routing.yaml) reached")
        request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
        headers = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        try:
            resp = self._post(API_URL, headers, request, spec.get("timeout_s", DEFAULT_TIMEOUT_S))
        except requests.RequestException as exc:
            atomic_json(result_path, {"status": "FAILED", "error": f"claude_api request failed: {exc}"})
            return {"kind": "claude_api", "result_path": str(result_path)}
        if credit_refusal(resp.status_code, resp.text):
            # Nothing was billed and nothing is wrong with the job: no result (recover() finds none, so nothing is
            # collected or escalated), the lane marked spent, the launch deferred or rerouted by the loop.
            raise LaneUnavailable(mark_credit_exhausted(layout, resp.text), f"claude_api: {resp.text[:300]}")
        if resp.status_code >= 400:
            atomic_json(result_path, {"status": "FAILED", "error": f"claude_api http {resp.status_code}: {resp.text[:500]}"})
            return {"kind": "claude_api", "result_path": str(result_path)}
        body = resp.json()
        atomic_json(saved, body)          # durable BEFORE the ledger and the result: one POST per attempt, ever
        clear_credit_exhausted(layout)    # a served request: credit is back
        return self._finish(spec, layout, request, body)

    def _finish(self, spec: dict, layout: Layout, request: dict, body: dict) -> dict:
        """Ledger entry, parse, `result.json` — for a response body that is already on disk. Re-entrant: every step
        is idempotent, so a crash anywhere inside it costs nothing but the work, never a second paid call."""
        result_path = Path(spec["run_dir"]) / "result.json"
        usage = body.get("usage") or {}
        record_usage(layout, spec["task_id"], spec["attempt_id"], request["model"], usage,
                     response_id=str(body.get("id") or ""))
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
                    "usage": usage, "model": request["model"], "result": obj})
        return {"kind": "claude_api", "result_path": str(result_path)}

    def recover(self, spec: dict) -> dict | None:
        """A finished result is adopted; a half-finished attempt (response persisted, result not) is deliberately
        NOT a handle — the loop relaunches, and submit() finishes from `response.json` without a second call."""
        p = Path(spec["run_dir"]) / "result.json"
        return {"kind": "claude_api", "result_path": str(p)} if p.exists() else None

    def alive(self, handle: dict) -> bool:
        return False

    def collect(self, spec: dict, handle: dict) -> Result | None:
        p = Path(handle["result_path"])
        if not p.exists():
            return Result(status="FAILED", error="claude_api left no result")
        payload = json.loads(p.read_text(encoding="utf-8"))
        # `raw` is the structured result object itself, exactly as claude_cli returns it: the delivery's declared
        # `tests` (loop.ingest), a review's verdict/findings (supervisor.review) and a diagnosis' decision are all read
        # off Result.raw, so an envelope here silently emptied every one of them. A response
        # that produced no structured object falls back to the envelope, which carries the error.
        obj = payload.get("result")
        return Result(status=payload.get("status", "FAILED"), artifacts=payload.get("artifacts", []),
                      notes=payload.get("notes", ""), error=payload.get("error"), model=payload.get("model"),
                      usage=payload.get("usage", {}), raw=obj if isinstance(obj, dict) else payload)


register(ClaudeApiAdapter())
