# Schema samples

One valid and one invalid document per schema. The framework's `tests/test_agent_cards_and_schemas.py` asserts that
each `*.valid.json` validates and each `*.invalid.json` does not, so the samples are the schemas' regression test —
a schema that stops rejecting its invalid sample has lost a constraint.

| Sample | Violations |
|---|---|
| `delivery.invalid.json` | `status: DONE` is outside the Result enum; the artifact is an absolute path rather than repository-relative (an absolute path escapes the owned-path manifest that verification compares); `decisions` is missing although the result contract requires it. |
| `review.invalid.json` | an `ACCEPT` carrying a blocking finding, and an `advisory` finding marked blocking on a non-`BLOCKED` verdict — the two cross-field rules `contract.review_result()` enforces. |
| `research_report.invalid.json` | an OBSERVATION with zero `source_ids`; `confidence: "very high"` outside the enum; a URL with no scheme; `retrieved` and `created` not parseable dates. |
| `assessment.invalid.json` | `independent: false`; a score of 7 above the 1-5 rubric; a repeated candidate in `scores` and in `ranking`; reasoning below the minimum length. |

A project that adds result kinds (a plugin) adds its schema here with a valid and an invalid sample.
