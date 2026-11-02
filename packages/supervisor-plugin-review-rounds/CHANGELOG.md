# Changelog

## 1.0.0

- Inbox kinds `round` and `labelling`, review commands, a `review` CLI, a `/review` route and its static
  assets, and a doctor check.
- An on-demand verdict migration (`supervisor review migrate-verdicts`) that is crash-safe: an interrupted run
  can simply be re-run to completion, and a completed item is never rewritten.

## 0.1.0

- Scaffolded by `supervisor plugin new review_rounds --package` (framework 2.0.0).
