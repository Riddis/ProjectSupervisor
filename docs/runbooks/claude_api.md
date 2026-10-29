# Runbook — `claude_api` adapter (optional, metered)

A single Messages API call per attempt, billed to an API key the owner put in `secrets/.env`
(`ANTHROPIC_API_KEY=...`). Off until `routing.yaml claude_api.monthly_budget_usd` is above 0 and the key exists.

- Same packet text as `claude_cli`, as one user message; same fenced JSON result expected back.
- **No tool use**: text in, text out. Suitable for reasoning/review/planning/documentation roles run unattended;
  file-writing engineering tasks stay on `claude_cli`.
- Synchronous inside `submit()`; `result.json` exists before it returns, so a crash never re-spends the call.
- Model: the role's short alias (`sonnet`/`opus`/`haiku`) mapped through `claude_api.MODEL_ALIASES`.
- Disabled vs. gated: a missing key or `monthly_budget_usd: 0` is *disabled* (FAILED with a clear error), not a
  wait. A reached `monthly_budget_usd` and a spent prepaid balance are *gated*: no model runs, nothing is billed,
  no attempt fails (`LaneUnavailable`, a capacity deferral). The budget gate lifts at the next calendar month.
- Spent balance: when the Console refuses a request with "credit balance is too low" the adapter writes
  `state/runtime/claude_api_credit.json` and the lane counts as having no credits until
  `claude_api.exhausted_recheck_hours` (default 24) have passed; then the next request tries again (a refusal
  costs nothing) and a served request deletes the file. While it is spent the unattended switch keeps work on
  `claude_cli`, and a delivery the switch had already moved here is closed as `REROUTED` (`rerouted_to`) and
  queued on its own lane — no escalation. Work pinned to this lane (`job.adapter`, `cost_category: paid`) and
  reviews wait PREPARED until the recheck instead. To retry sooner (credit topped up, a new billing cycle):
  `supervisor cmd lane.reset --payload '{"lane":"claude_api"}'` — the waiting records are released on the loop's next
  iteration, as they are when the owner raises a reached `monthly_budget_usd`.
- Detection is exact: HTTP 400 with a JSON `error.type` of `invalid_request_error` and the "credit balance is too
  low" message. Any other response, whatever it contains, stays an ordinary FAILED attempt.
- Ledger: `state/runtime/cost_ledger.jsonl`, one line per call, priced from `claude_api.pricing_usd_per_mtok`
  (fill in current rates or the dollar figures are 0). `spent_this_month` vs `monthly_budget_usd` decides
  `credits_remaining`. The template ships `monthly_budget_usd: 0` (lane off); if the key is deleted from
  `routing.yaml` altogether the adapter falls back to `claude_api.DEFAULT_BUDGET_USD` (100), so keep the line
  and set the figure the owner actually approved.
- Routing: `cost_category: paid` or `job.adapter: claude_api` always uses it; `supervisor run --unattended` with
  `capacity.unattended_prefers_api_credits: true` switches `claude_cli` picks to it while credits remain.
