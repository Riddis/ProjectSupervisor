# Runbook — credential policy

`config/policy.yaml` is the data; `supervisor/policy.py` is the only mechanism. Everything fails closed.

## The guards

| Guard | Boundary | Behaviour |
|---|---|---|
| Credentials never reach a packet | `supervisor/packets.py` and every agent adapter's `prepare()` | **refuses** (`policy.PolicyViolation`); the attempt fails visibly |
| Credentials never reach a log | `supervisor/tasks.py log()` — the single append path | **redacts** to `[REDACTED]`; the record is kept |
| Credentials never reach a child | `adapters/process.scrub_env()` | strips every variable named like a credential |
| Project-specific content rules | extension `packet_screen`s | **refuse**, same as the credential screen |

A refusal names labels (`secrets/.env:ANTHROPIC_API_KEY`, `pattern/github_token`, `env/MY_TOKEN`), never the value.

## What counts as a credential

- A value matching a shape pattern (`secrets.patterns`: Anthropic/OpenAI/GitHub/Slack/AWS/Google keys, JWTs, PEM
  blocks, bearer headers, `X_SECRET=...` assignments);
- any value in `secrets/` whose key or file name is *named* like one (`secrets.env_key_suffixes`,
  `secrets.secret_key_words`); every value in a dedicated credential file (`service_token.json`);
- any variable of the supervisor's own environment whose name ends in a secret suffix.

Non-secret configuration (an account id, a base URL) belongs in `config/`, not `secrets/`: a brief may need to quote
it, and `secrets/` values are refused in briefs.

## How the config can and cannot move

Lists (`patterns`, `secret_key_words`, `env_key_suffixes`) are unioned with the built-in floor — an edit can only add.
`min_value_length` can only be lowered; `ignore_files` can never hide a dotenv file; an empty
`redaction_placeholder` falls back to the built-in. A wrongly-typed entry (`patterns: null`) is dropped, the floor
stands, and `policy.config_problems()` names it — `supervisor doctor` shows them as the `policy` check (ok when the
list is empty). Sections the floor does not know (a project's own) are taken as given for extensions to read with
`policy.section(name)`.

## Adding a project rule

Register a packet screen in an extension:

```python
def setup(registry):
    def no_customer_data(layout, text, where):
        if "CUSTOMER-RECORD" in text:
            raise PolicyViolation(f"{where} refused: customer records never leave this machine")
    registry.packet_screen(no_customer_data)
```

A screen is only a protection while its extension loads. If the extension fails to import (a typo, a missing
dependency), `assert_packet_clean()` refuses **every** packet naming the extension and the loop refuses to start
(runtime `BLOCKED`, reason `required extension(s) failed to load: <name>: ...`); `supervisor doctor` shows the
error under `extensions`. Fix the module, or — only for an extension whose absence is genuinely harmless — list it
as `{name: <name>, required: false}` in `project.yaml` (docs/FRAMEWORK_VS_EXTENSION.md).
