"""Packet construction: the brief, frozen inputs and result schema for a dispatch. Role cards and
the delivery brief are shared with the claude_cli/codex_cli adapters; review and revision briefs live here
because review dispatch and automatic revision are the supervisor's, not an adapter's.

A review packet freezes the reviewed inputs (sha256 manifest) and gives the reviewer the original contract plus
the producer's delivered artifacts — never the producer's own notes/self-assessment. A revision packet quotes the failure verbatim so the next attempt sees the
actual evidence, not a paraphrase."""
from __future__ import annotations

import json

import yaml

from . import policy, verify
from ._vendor.contract import manifest
from .models.task import Task
from .paths import Layout


def result_contract_delivery(layout: Layout, task: Task, model_lane: bool = True) -> str:
    """The `## Result contract` block, rendered for this task's own case (G1/G3/G5, 2.4.0): whether a command is
    required, the project's configured limits, and its own allowed command list — never one constant string for
    every task."""
    requires = task.requires_commands(model_lane)
    evidence_sentence = (
        "This task needs command evidence: your result must report at least one command in `tests`. If every "
        "criterion is about a document, write a small pytest under your run directory that reads that document "
        "read-only and asserts what the criteria say, run it, and report it."
        if requires else
        "No command is required for this task. Report `tests: []`. Any command you do report is re-run here and "
        "must pass."
    )
    lim = verify.limits(layout)
    allowed = ", ".join(f"`{c}`" for c in verify.allowed_test_commands(layout))
    return (
        "## Result contract\nWhen finished, end your answer with exactly one fenced JSON block:\n"
        '```json\n{"kind":"delivery","schema_version":1,"status":"COMPLETE|PARTIAL|BLOCKED","artifacts":["relative/paths"],'
        '"tests":[{"command":"python -m pytest tests/test_x.py","exit_code":0}],"notes":"one paragraph","decisions":[]}\n```\n'
        f"{evidence_sentence}\n"
        f"Each command must finish in **{lim['per_command_timeout_s']} s** and all of them together in "
        f"**{lim['budget_s']} s**; at most **{lim['max_commands']}** commands are re-run. A full suite that cannot "
        "finish inside that is saved as a log under your run directory and cited in `notes` — never declared as a "
        "command.\n"
        f"Only these commands are re-run, matched by prefix: {allowed}. A helper script is never a command — wrap "
        "what it asserts in a pytest.\n")


def role_card(layout: Layout, role: str) -> str:
    """Prompt card text for a role, or '' while the role is dormant/undefined."""
    cfg = layout.config / "routing.yaml"
    if not cfg.exists():
        return ""
    roles = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("roles") or {}
    card = (roles.get(role) or {}).get("card")
    if not card:
        return ""
    p = layout.agents / card
    return p.read_text(encoding="utf-8") if p.exists() else ""


def frozen_inputs(layout: Layout, paths: list[str]) -> dict:
    """sha256 manifest of the given repo-relative paths, scoped to those that exist — a claimed-but-absent
    artifact is left out, never recorded as {"kind": "missing"}, and review_brief() documents that scoping so a
    reviewer rehashing the list reproduces this digest. A review bound to this manifest is invalidated the moment
    any of it changes."""
    existing = [p for p in paths if (layout.root / p).exists()]
    return manifest(existing, root=layout.root) if existing else {}


def _checks_block(task: Task) -> str:
    if not task.checks:
        return ""
    return ("\n\n## Required checks (fixed by the contract; the supervisor re-runs these, you cannot replace them)\n"
            + "\n".join(f"- `{c.command}`" for c in task.checks))


def _test_roots_note(layout: Layout, task: Task) -> str:
    """Where a declared test command re-runs (G2, 2.4.0): named roots when the task sets any, else the one
    sentence that is true when it does not."""
    if task.test_roots:
        example = task.test_roots[0]
        return ("\n\n**Where your test commands re-run.** Verification re-runs each command you report from "
                f"`{layout.root}` by default. A command that must run in another directory names it as `cwd` in "
                "its own entry, and only these are allowed: " + ", ".join(task.test_roots) + ". The interpreter is "
                "chosen here — that directory's `.venv` if it has one, otherwise the supervisor's own — so what "
                "you write as the first word of the command does not select it.\n"
                '`{"command": "python -m pytest tests/test_widget.py", "cwd": ' + json.dumps(example) + ', "exit_code": 0}`')
    return f"\n\nVerification re-runs each command from `{layout.root}` with the supervisor's own interpreter."


def _ownership_block(layout: Layout, task: Task) -> str:
    if task.exclusive() or not task.owned_paths:
        return ("\n\n## Write scope\nExclusive: you may edit anything in the repository; every change you make must be "
                "listed in `artifacts` (undeclared writes fail verification)." + _test_roots_note(layout, task))
    return ("\n\n## Write scope\nOnly these paths (anything written outside them fails verification):\n"
            + "\n".join(f"- {p}" for p in task.owned_paths) + _test_roots_note(layout, task))


GUIDANCE_HEADING = "## Guidance (read before starting; earlier attempts failed or were stopped)"
REVIEW_GUIDANCE_HEADING = "## Owner guidance for this review"


def _guidance_items(task: Task, scope: str, digest: str | None = None) -> list[dict]:
    """The task's `guidance` entries for one packet kind. A diagnosis requeue (no `scope`) and an owner REQUEUE of a
    task or a delivery address the next delivery; an owner REQUEUE of a review addresses the review of the digest it
    was given for (a new delivery makes it moot)."""
    out = []
    for g in (task.model_extra or {}).get("guidance") or []:
        if not isinstance(g, dict) or not str(g.get("text") or "").strip():
            continue
        if (g.get("scope") or "delivery") != scope:
            continue
        if scope == "review" and g.get("digest") and g.get("digest") != digest:
            continue
        out.append(g)
    return out[-3:]


def _format_guidance(items: list[dict]) -> str:
    return "\n\n".join(f"**{g.get('by', 'diagnosis')}** ({str(g.get('at', ''))[:10]}): {g.get('text', '')}" for g in items)


def guidance_block(task: Task) -> str:
    """Guidance for the next delivery attempt — written by a diagnosis requeue or by the owner's REQUEUE text after
    automatic retries stopped: the next attempt must read it."""
    items = _guidance_items(task, "delivery")
    return f"\n\n{GUIDANCE_HEADING}\n" + _format_guidance(items) if items else ""


_guidance_block = guidance_block                     # pre-1.4.1 name


def with_guidance(task: Task, prompt: str) -> str:
    """A delivery prompt that did not come from delivery_brief (a job's own `prompt`/`prompt_file`, an adapter's
    fallback) still carries the guidance: appended unless the prompt already has the block."""
    block = guidance_block(task)
    return prompt if not block or GUIDANCE_HEADING in prompt else prompt + block


NON_DELIVERY_KINDS = ("review", "diagnosis", "conversation")     # packets that never carry delivery guidance


def worker_prompt(task: Task, job: dict, prompt: str) -> str:
    """The prompt an adapter sends for this job: a delivery-kind job gets the task's guidance (with_guidance); a
    review, diagnosis or chat turn is sent as built."""
    return prompt if job.get("result_kind", "delivery") in NON_DELIVERY_KINDS else with_guidance(task, prompt)


def review_guidance_block(task: Task, digest: str) -> str:
    items = _guidance_items(task, "review", digest)
    if not items:
        return ""
    return (f"\n\n{REVIEW_GUIDANCE_HEADING}\nThe owner re-queued this review after its retry budget was spent and "
            "added the instructions below. Apply them when judging the delivery against the contract.\n"
            + _format_guidance(items))


def _tests_heading(task: Task) -> str:
    """G1, 2.4.0: the Tests section heading names whether a command is required — delivery_brief is only ever
    sent on a model lane (escalation.dispatch_revision, the claude_cli/claude_api adapters), so `model_lane=True`."""
    return ("## Tests (at least one of these must be a command the supervisor can re-run)" if task.requires_commands(True)
            else "## Tests (judged by reading the artifacts; no command is required)")


def delivery_brief(layout: Layout, task: Task) -> str:
    card = role_card(layout, task.owner_role)
    return policy.assert_packet_clean(((card + "\n\n") if card else "") + (
        f"# Task {task.id}: {task.title}\n\nRole: {task.owner_role}\nRepository: {layout.root}\n\n"
        f"## Objective\n{task.objective}\n\n## Actions\n" + "\n".join(f"- {a}" for a in task.actions) +
        "\n\n## Expected artifacts (all of these; you may add more, never fewer)\n" + "\n".join(f"- {a}" for a in task.artifacts) +
        "\n\nFiles under `runs/` are this attempt's own working notes (handoff, logs, evidence): never list them in "
        "`artifacts` — they are dropped from the delivery. Cite their paths in `notes` instead.\n\n" +
        _tests_heading(task) + "\n" + "\n".join(f"- {t}" for t in task.tests) + _checks_block(task) +
        _ownership_block(layout, task) + _guidance_block(task) +
        f"\n\n## Acceptance\n{task.acceptance}\n\n" + result_contract_delivery(layout, task)),
        layout, f"delivery packet {task.id}")


def contract_block(task: Task) -> str:
    """The task contract a reviewer judges against — never the producer's own notes/self-assessment."""
    return (f"## Objective\n{task.objective}\n\n## Acceptance\n{task.acceptance}\n\n## Declared tests\n"
            + "\n".join(f"- {t}" for t in task.tests) + "\n"
            + ("## Required checks\n" + "\n".join(f"- `{c.command}`" for c in task.checks) + "\n" if task.checks else ""))


def _verification_summary(layout: Layout, task: Task, digest: str) -> str:
    """The supervisor's own verification record for this digest, quoted verbatim. A read-only review lane has no
    Bash and no writable scratch directory, so it cannot re-run the declared tests itself; hand it the
    evidence instead of leaving it to discover it cannot execute anything."""
    v = task.delivery.verification
    if not v or v.digest != digest:
        return ""
    tests = []
    if v.report and (layout.root / v.report).exists():
        try:
            tests = json.loads((layout.root / v.report).read_text(encoding="utf-8")).get("tests") or []
        except (OSError, ValueError):
            tests = []
    lines = [f"- ok: {v.ok}", f"- detail: {v.detail}"] + [f"- `{t.get('command')}` exit {t.get('exit_code')}" for t in tests]
    return ("\n## Verification already performed\nThe supervisor already ran the declared tests for this digest "
           "and recorded the result below; re-running them yourself is optional, and some review lanes are "
           "read-only with no writable scratch directory and cannot execute anything at all — that is not a "
           "defect in the delivery.\n" + "\n".join(lines) + "\n")


# The vocabulary _vendor/contract.review_result accepts; told to the reviewer so a verdict is not lost over a
# category spelling the contract refuses.
FINDING_CATEGORY_LIST = "correctness | requirement | regression | architecture | quality | advisory"


def review_brief(layout: Layout, task: Task, digest: str, artifacts: list[str], role: str) -> str:
    card = role_card(layout, role)
    example = json.dumps({"kind": "review", "schema_version": 1, "verdict": "ACCEPT|REVISE|REJECT|BLOCKED",
                          "findings": [], "summary": "...", "reviewed": artifacts, "digest": digest})
    return policy.assert_packet_clean(((card + "\n\n") if card else "") + (
        f"# Review {task.id}: {task.title}\n\nRepository: {layout.root}\n\n"
        f"You are reviewing frozen inputs bound to digest `{digest}`. If any of the listed files change before "
        "you finish, your verdict will be discarded and a fresh review dispatched against the new digest. This "
        "digest is sha256 of a JSON manifest built only from the exact paths listed below under 'Delivered "
        "artifacts': each keyed by its root-relative POSIX path, each file recorded as the three-key object "
        "{\"kind\": \"file\", \"size\": <byte size>, \"sha256\": <hex digest of its bytes>} — a listed path that "
        "does not exist when the manifest is taken is left out of it entirely rather than recorded as missing, and "
        "a directory is {\"kind\": \"directory\"} followed by its children visited the same way — the manifest "
        "dumped with sorted keys and no extra whitespace (json.dumps(..., sort_keys=True, "
        "separators=(\",\", \":\"))), then hashed again. Hashing anything outside this list (another file you "
        "also read, a schema, a prior HANDOFF.md), adding an entry for a listed path that is not on disk, "
        "omitting the \"kind\" key, or using a different order or path form, will not reproduce it — a mismatch "
        "there is not evidence the frozen inputs are wrong.\n\n"
        + contract_block(task) +
        "\n## Delivered artifacts (frozen inputs — read these, not the producer's notes)\n"
        + "\n".join(f"- {a}" for a in artifacts) +
        _verification_summary(layout, task, digest) + review_guidance_block(task, digest) +
        "\n\n## Result contract\nEnd your answer with exactly one fenced JSON block:\n```json\n" + example + "\n```\n"
        "Each entry in `findings` is an object {\"category\": one of " + FINDING_CATEGORY_LIST + ", \"blocking\": "
        "true|false, \"message\": \"...\", \"evidence\": \"file:line, command output or quote\"}. Use exactly those "
        "category names. `advisory` findings are never blocking; an ACCEPT carries no blocking finding.\n"),
        layout, f"review packet {task.id}")


def revision_brief(task: Task, prior_brief: str, source: str, detail: str) -> str:
    """Appended ahead of the next delivery attempt after a REVISE/failed verification: the failure quoted
    verbatim, never softened. The quoted failure is the one piece of packet text the supervisor did not
    author — a reviewer or a test output could have echoed a credential into it — so it is screened too."""
    return policy.assert_packet_clean(
        prior_brief + f"\n\n## Revision required\nThe previous attempt was rejected ({source}):\n\n"
        f"```\n{detail[:4000]}\n```\n\nFix this. Do not restate the original brief back; show what changed.\n",
        None, f"revision packet {task.id}")
