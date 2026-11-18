"""Packet construction: the brief, frozen inputs and result schema for a dispatch. Role cards and
the delivery brief are shared with the claude_cli/codex_cli adapters; review and revision briefs live here
because review dispatch and automatic revision are the supervisor's, not an adapter's.

A review packet freezes the reviewed inputs (sha256 manifest) and gives the reviewer the original contract plus
the producer's delivered artifacts — never the producer's own notes/self-assessment. A revision packet quotes the failure verbatim so the next attempt sees the
actual evidence, not a paraphrase."""
from __future__ import annotations

import difflib
import json

import yaml

from . import jobs as jobs_mod
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

# The cap bounding which text artifacts a review pass snapshots for a later diff (ATTEMPT_MODEL.md 4.3): a mid-task
# attempt is never committed to git (only a COMPLETE task's bytes are checkpointed, checkpoint.py), so the only
# durable copy of what a prior pass actually saw is the one its own job record keeps — kept small on purpose.
TEXT_SNAPSHOT_CAP = 20_000
# The cap bounding how much of the combined unified diff a review packet quotes; a pass that changed more than
# this is still named in the artifact-level change list, just not diffed line by line in the prompt itself.
DIGEST_DIFF_CAP = 8_000

ROUND_1_INSTRUCTION = ("This is the first review pass for this delivery's current digest. Report every blocking "
                      "finding you find in this pass, not only the first one you hit — stopping early just "
                      "costs another pass to find the next defect.")


def _text_or_none(layout: Layout, rel: str, cap: int) -> str | None:
    p = layout.root / rel
    try:
        if not p.is_file() or p.stat().st_size > cap:
            return None
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def text_snapshot(layout: Layout, artifacts: list[str]) -> dict:
    """path -> text, for every listed artifact that exists, is a plain file, decodes as UTF-8 and is within
    `TEXT_SNAPSHOT_CAP`. Kept on the dispatching review's own job record (review.dispatch) so a later round can
    diff against it without ever reading a run directory."""
    return {rel: text for rel in artifacts if (text := _text_or_none(layout, rel, TEXT_SNAPSHOT_CAP)) is not None}


def _change_list(prior_manifest: dict, current_manifest: dict) -> list[dict]:
    """Added/removed/changed, by path, between two frozen_inputs() manifests (hash+size — never file content)."""
    out = []
    for path in sorted(set(prior_manifest) | set(current_manifest)):
        before, after = prior_manifest.get(path), current_manifest.get(path)
        if before is None:
            out.append({"path": path, "change": "added"})
        elif after is None:
            out.append({"path": path, "change": "removed"})
        elif before != after:
            out.append({"path": path, "change": "changed"})
    return out


def _unified_diffs(layout: Layout, prior_snapshot: dict, change_list: list[dict]) -> str:
    """A unified diff per changed artifact for which both the previous round's own text snapshot and a current,
    in-cap text reading exist — never for a path only one side can show as text (binary, oversized, or added/
    removed, which the change list already names on its own)."""
    changed = [c["path"] for c in change_list if c["change"] == "changed" and c["path"] in prior_snapshot]
    parts = []
    for rel in changed:
        current = _text_or_none(layout, rel, TEXT_SNAPSHOT_CAP)
        if current is None:
            continue
        diff = "".join(difflib.unified_diff(prior_snapshot[rel].splitlines(keepends=True),
                                            current.splitlines(keepends=True),
                                            fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        if diff:
            parts.append(diff)
    return "".join(parts)


def _prior_rounds(task: Task, digest: str) -> list[tuple[str, list]]:
    """(digest, ReviewRecords) for every digest this task's history (`task.delivery.reviews`, the store) has ever
    reviewed, oldest first, excluding the digest now under review — a round groups every role's verdict for the
    same past digest, however many roles were required."""
    order: list[str] = []
    by_digest: dict[str, list] = {}
    for r in task.delivery.reviews:
        if r.digest == digest:
            continue
        by_digest.setdefault(r.digest, []).append(r)
        if r.digest not in order:
            order.append(r.digest)
    return [(d, by_digest[d]) for d in order]


def prior_round_context(layout: Layout, task: Task, digest: str, artifacts: list[str]) -> dict:
    """Everything a review packet needs about earlier rounds of this task (ATTEMPT_MODEL.md 4.3, points 2-4): the
    findings and verdict of every previous round, what the producer's own delivery result claims about them, the
    artifact-level change list and a size-capped unified diff against the immediately preceding round — entirely
    from the task document and this task's job records (both the store), plus the current working tree; never a
    run directory. `snapshot` is this round's own text snapshot, for the round after THIS one to diff against."""
    rounds = [{"round": n, "attempt": jobs_mod.attempt_for_digest(layout, task.id, past_digest), "digest": past_digest,
              "reviews": [{"role": r.role, "verdict": r.verdict.value, "findings": list(r.findings)} for r in records]}
             for n, (past_digest, records) in enumerate(_prior_rounds(task, digest), start=1)]
    # The producer's own account of THIS delivery is the response to the round immediately before it; an earlier
    # round's own notes do not survive being superseded (task.result is overwritten on every delivery, R-15) — so
    # only the latest previous round ever has one, and it is said plainly rather than invented for the rest.
    claim = task.result if rounds else None
    # `responses`: the producer's own optional, per-finding reply (point 3), keyed by the finding id it answers —
    # `claim` (task.result) stays the one free-text account read for a finding this carries none of, or when a
    # producer gives none of these at all.
    responses = {str(r["id"]): {"status": r.get("status"), "note": r.get("note")}
                for r in (task.findings_response or []) if isinstance(r, dict) and r.get("id")} if rounds else {}
    change_list: list[dict] = []
    diff_text = ""
    if rounds:
        prior_digest = rounds[-1]["digest"]
        prior_manifest, prior_snapshot = {}, {}
        for rec in jobs_mod.for_task(layout, task.id):
            if jobs_mod.kind_of(rec) == "review" and rec.get("review_digest") == prior_digest:
                prior_manifest = rec.get("review_frozen") or prior_manifest
                prior_snapshot = rec.get("review_text_snapshot") or prior_snapshot
        change_list = _change_list(prior_manifest, frozen_inputs(layout, artifacts))
        diff_text = _unified_diffs(layout, prior_snapshot, change_list)
    return {"rounds": rounds, "claim": claim, "responses": responses, "change_list": change_list,
            "diff_text": diff_text, "snapshot": text_snapshot(layout, artifacts)}


def _finding_line(f: dict, responses: dict | None = None) -> str:
    tag = "blocking" if f.get("blocking") else "advisory"
    resp = (responses or {}).get(f.get("id"))
    reply = ""
    if resp:
        reply = f" — producer: {resp.get('status')}" + (f" ({resp['note']})" if resp.get("note") else "")
    return (f"  - `{f.get('id', '')}` [{f.get('category', '')}, {tag}]: {f.get('message', '')} "
            f"(evidence: {f.get('evidence', '')}){reply}")


def prior_round_block(ctx: dict) -> str:
    """The text ATTEMPT_MODEL.md 4.3 adds to a review packet from `prior_round_context()`."""
    if not ctx["rounds"]:
        return f"\n\n## Round 1\n{ROUND_1_INSTRUCTION}\n"
    lines = ["\n\n## Previous rounds of this task (read before judging this delivery)"]
    for rnd in ctx["rounds"]:
        attempt = rnd["attempt"] if rnd["attempt"] is not None else "—"
        lines.append(f"\n### Round {rnd['round']} (attempt {attempt}, digest {rnd['digest'][:12]})")
        for rv in rnd["reviews"]:
            lines.append(f"- **{rv['role']}** verdict {rv['verdict']}")
            lines.extend(_finding_line(f, ctx["responses"]) for f in rv["findings"])
    lines.append("\n**What the producer's current delivery result claims about the above:**\n"
                 + (ctx["claim"] or "no notes were recorded."))
    if ctx["change_list"]:
        lines.append("\n**Artifact-level changes since the previous round:**\n"
                     + "\n".join(f"- {c['path']}: {c['change']}" for c in ctx["change_list"]))
    if ctx["diff_text"]:
        capped = ctx["diff_text"][:DIGEST_DIFF_CAP]
        note = "" if len(ctx["diff_text"]) <= DIGEST_DIFF_CAP else "\n(truncated at the packet's own size cap)"
        lines.append(f"\n**Unified diff of changed text artifacts:**\n```diff\n{capped}{note}\n```")
    return "\n".join(lines)


def _review_note_block(task: Task) -> str:
    if not task.review_note:
        return ""
    return f"\n\n## Owner's guidance for reviewers\n{task.review_note}\n"


def review_brief(layout: Layout, task: Task, digest: str, artifacts: list[str], role: str, ctx: dict | None = None) -> str:
    """`ctx`, when given, is this call's own `prior_round_context()` result, already computed by the caller (e.g.
    `review.dispatch()`, which also needs it for the dispatch extra) — passed through rather than built again here
    a second time for the same dispatch. Built fresh when omitted, as every existing caller still does."""
    card = role_card(layout, role)
    example = json.dumps({"kind": "review", "schema_version": 1, "verdict": "ACCEPT|REVISE|REJECT|BLOCKED",
                          "findings": [], "summary": "...", "reviewed": artifacts, "digest": digest})
    ctx = ctx if ctx is not None else prior_round_context(layout, task, digest, artifacts)
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
        _review_note_block(task) + prior_round_block(ctx) +
        "\n\n## Result contract\nEnd your answer with exactly one fenced JSON block:\n```json\n" + example + "\n```\n"
        "Each entry in `findings` is an object {\"category\": one of " + FINDING_CATEGORY_LIST + ", \"blocking\": "
        "true|false, \"message\": \"...\", \"evidence\": \"file:line, command output or quote\", \"id\": \"...\"}. "
        "Use exactly those category names. `advisory` findings are never blocking; an ACCEPT carries no blocking "
        "finding. `id` is optional — name the same id again when a finding repeats from a previous round shown "
        "above; omit it on a new finding and one is derived for you.\n"),
        layout, f"review packet {task.id}")


def revision_brief(task: Task, prior_brief: str, source: str, detail: str) -> str:
    """Appended ahead of the next delivery attempt after a REVISE/failed verification: the failure quoted
    verbatim, never softened. The quoted failure is the one piece of packet text the supervisor did not
    author — a reviewer or a test output could have echoed a credential into it — so it is screened too."""
    return policy.assert_packet_clean(
        prior_brief + f"\n\n## Revision required\nThe previous attempt was rejected ({source}):\n\n"
        f"```\n{detail[:4000]}\n```\n\nFix this. Do not restate the original brief back; show what changed.\n",
        None, f"revision packet {task.id}")
