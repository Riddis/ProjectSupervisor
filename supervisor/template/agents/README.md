# agents/

A role is a card plus a routing entry (`config/routing.yaml`). Nothing else makes a role, and an inactive role
costs nothing.

- `<role>.md` — the role card: mandate, authority, escalation path, how the role works, what it delivers, and the
  lines it does not cross. Cards are short on purpose (≤ 400 words): the packet builder prepends the card to every
  dispatch for that role, so length is paid for on every turn.
- `schemas/*.json` — the result and document schemas (draft 2020-12). An agent's fenced JSON result is validated
  against the schema named by its `result_kind`.
- `schemas/samples/` — one valid and one invalid document per schema (see `schemas/samples/README.md`).
- `_owner_note_protocol.md` — appended to every brief: how a running agent picks up a note the owner drops from the
  Control Center.

## Cards name no models

A card describes judgement; `config/routing.yaml` decides which provider, model, effort and tool set serves it. That
split is the point: the same card can run on a different lane next month without being rewritten, and a role can
never argue for a model. The framework's test suite fails if a model or vendor name appears in a card.

Where independence matters, the card says "a different vendor lane than the implementer" rather than naming the lane.

## The starting roles

The template ships five generic roles — `senior_pm` (priorities, decomposition, proposals), `chief_architect`
(design, standards, adjudication), `engineer` (implementation), `code_review` (independent review on a different
vendor lane), `research_strategist` (evidence) — plus `owner` (the human; inbox only, no model, no card) and
`conversation` (the Control Center chat lane, not an org-chart role).

The setup skill adds, removes or renames roles for the project at hand: a project that ships documents instead of
code may want a `writer` and an `editor`; a project with a domain tool may want a specialist role whose card names the
tool's conventions. Every role you add needs a card here, a routing entry, and `result_kinds` it is allowed to
produce. Dormant roles keep `card: null`, `active: false` and a `note` saying when the card comes.

## Result kinds

| Schema | Written by | Read by |
|---|---|---|
| `delivery.json` | every implementing role | verification, `tasks.advance()` |
| `review.json` | `code_review` and any named reviewer role | review records, escalation |
| `research_report.json` | `research_strategist` | reviewers, the owner |
| `assessment.json` | any role asked to score candidates independently | the manager's reconciliation |

Each role's permitted kinds are listed as `result_kinds` in its routing entry.
