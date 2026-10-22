# Runbook — `claude_api` adapter (optional, metered)

A single Messages API call per attempt, billed to an API key the owner put in `secrets/.env`
(`ANTHROPIC_API_KEY=...`). Off until `routing.yaml claude_api.monthly_budget_usd` is above 0 and the key exists.

- Same packet text as `claude_cli`, as one user message; same fenced JSON result expected back.
- **No tool use**: text in, text out. Suitable for reasoning/review/planning/documentation roles run unattended;
  file-writing engineering tasks stay on `claude_cli`.
- Synchronous inside `submit()`; `result.json` exists before it returns, so a crash never re-spends the call.
- Model: the role's short alias (`sonnet`/`opus`/`haiku`) mapped through `claude_api.MODEL_ALIASES`.
- Disabled vs. gated: a missing key or exhausted budget is *disabled* (FAILED with a clear error), not a wait.
- Ledger: `state/runtime/cost_ledger.jsonl`, one line per call, priced from `claude_api.pricing_usd_per_mtok`
  (fill in current rates or the dollar figures are 0). `spent_this_month` vs `monthly_budget_usd` decides
  `credits_remaining`. The template ships `monthly_budget_usd: 0` (lane off); if the key is deleted from
  `routing.yaml` altogether the adapter falls back to `claude_api.DEFAULT_BUDGET_USD` (100), so keep the line
  and set the figure the owner actually approved.
- Routing: `cost_category: paid` or `job.adapter: claude_api` always uses it; `supervisor run --unattended` with
  `capacity.unattended_prefers_api_credits: true` switches `claude_cli` picks to it while credits remain.
