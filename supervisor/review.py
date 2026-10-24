"""Review dispatch: `reviewer` on a task names a role, not a person. For every required role with no
live review bound to the task's current delivery digest, build a review packet on frozen inputs and dispatch to
that role's lane (cross-vendor by construction — code_review's primary adapter is codex_cli while the implementer
is claude_cli — with a same-vendor fallback, flagged, when the primary engine's capacity is out).

If the frozen digest moves before a dispatched review comes back (a new delivery arrived), the verdict is
discarded here (never recorded) and `pending()` queues a fresh review against the new digest on the next
iteration — nothing special to undo, tasks.record_review()'s own digest check would refuse it anyway.
"""
from __future__ import annotations

from pathlib import Path

from . import gate, jobs as jobs_mod
from . import tasks as tasks_mod
from ._vendor.contract import CapacityDeferred, digest as digest_of, read_json
from .adapters import REGISTRY
from .models.task import ReviewVerdict, Task, TaskStatus

VENDOR_OF_ADAPTER = {"codex_cli": "codex", "claude_cli": "claude", "claude_api": "claude"}

# A reviewer must not be able to write the repository it is judging, and the digest check does not cover it: a write
# outside the delivered artifacts leaves the digest intact and the ACCEPT stands.
# The review lane therefore narrows the job itself, independently of the role's ordinary (writing) profile — several
# roles both build and review (chief_architect, senior_pm). Defaults: read the repository, write only this attempt's
# run dir, which is what the continuity protocol needs for HANDOFF.md. A codex lane has no per-path write mode, so it
# runs read-only and its handoff is prose in result.txt (_vendor/handoff.instructions, last line).
# Overridable per role as data: routing.yaml roles.<role>.review: {tools: ..., sandbox: ..., write_scope: ...}.
REVIEW_LANE = {"tools": "reviewer", "sandbox": "read-only", "write_scope": "run_dir"}
LANE_KEYS = ("tools", "sandbox", "write_scope")


def _role_profile(layout, role: str) -> dict:
    from .adapters.claude_cli import role_profile
    return role_profile(layout, role)


def review_lane(layout, role: str) -> dict:
    """Confinement for this role's review job (routing.yaml roles.<role>.review over REVIEW_LANE)."""
    configured = (_role_profile(layout, role).get("review") or {})
    return {**REVIEW_LANE, **{k: v for k, v in configured.items() if k in LANE_KEYS}}


MAX_INVALIDATIONS = 2    # also the cap on BLOCKED interruptions per (task, role, digest) before the task is BLOCKED


def frozen(layout, paths) -> dict:
    """packets.frozen_inputs() that never raises: contract.manifest rejects links, and the same material yields the
    same sentinel at dispatch and at ingest, so the comparison still holds (such a delivery fails verification)."""
    from . import packets as packets_mod
    try:
        return packets_mod.frozen_inputs(layout, [str(p) for p in paths])
    except ValueError as exc:
        return {"_unhashable": {"error": str(exc)[:200]}}


def lane_for(layout, role: str) -> tuple[str, dict, bool]:
    """(adapter_name, {model/effort overrides}, same_vendor_flag) for a role's review lane. Falls back to the
    role's configured same-vendor lane on a capacity deferral; propagates CapacityDeferred if there is none."""
    from . import capacity
    prof = _role_profile(layout, role)
    adapter_name = prof.get("adapter", "claude_cli")
    engine = getattr(REGISTRY.get(adapter_name), "engine", None) if adapter_name in REGISTRY else None
    if engine:
        try:
            capacity.require(layout, engine)
        except CapacityDeferred:
            fb = prof.get("fallback")
            if not fb:
                raise
            return fb["adapter"], {k: v for k, v in fb.items() if k in ("model", "effort")}, fb.get("flag") == "same_vendor"
    return adapter_name, {}, False


def _review_task_view(layout, task: Task, role: str, adapter_name: str, brief: str, overrides: dict) -> Task:
    doc = task.model_dump(mode="json")
    doc["owner_role"] = role
    doc["job"] = {"adapter": adapter_name, "role": role, "prompt": brief, "result_kind": "review",
                  **review_lane(layout, role), **overrides}
    return Task.model_validate(doc)


def pending(layout, tasks: dict[str, Task] | None = None) -> list[tuple[Task, str]]:
    """(task, role) pairs needing a fresh review dispatch: WAITING_FOR_REVIEW, no live review for the current
    digest, no review job already in flight for this task+role."""
    tasks = tasks or tasks_mod.load_all(layout)
    in_flight = {(r["task_id"], r.get("review_role")) for r in jobs_mod.open_records(layout)
                if (r.get("spec") or {}).get("kind") == "review"}
    out = []
    for t in tasks.values():
        if t.status != TaskStatus.WAITING_FOR_REVIEW or not t.delivery.digest:
            continue
        live = {r.role for r in t.delivery.reviews if r.digest == t.delivery.digest and not r.superseded}
        for role in t.reviewer:
            if role in live or (t.id, role) in in_flight:
                continue
            if gate.role_inactive(layout, role):
                _skip_once(layout, t, role)
                continue
            out.append((t, role))
    return out


_skips_logged: set[tuple[str, str]] = set()


def _skip_once(layout, task: Task, role: str) -> None:
    """A review that cannot be dispatched because its reviewer role is `active: false`: logged once per process (the
    condition persists until the owner edits routing.yaml; the task waits in WAITING_FOR_REVIEW)."""
    key = (task.id, role)
    if key in _skips_logged:
        return
    _skips_logged.add(key)
    tasks_mod.log(layout, "review_skip", "loop", task=task.id, role=role, reason=gate.inactive_reason(role))


def dispatch(supervisor, task: Task, role: str) -> dict | None:
    """Dispatch one review attempt, or None if the lane's capacity is out and there is no fallback (token-free
    wait, exactly like a primary dispatch deferral — nothing is consumed)."""
    layout = supervisor.layout
    from . import packets as packets_mod
    try:
        adapter_name, overrides, same_vendor = lane_for(layout, role)
    except CapacityDeferred:
        return None
    digest = task.delivery.digest
    artifacts = task.paths or task.artifacts
    brief = packets_mod.review_brief(layout, task, digest, artifacts, role)
    review_task = _review_task_view(layout, task, role, adapter_name, brief, overrides)
    note = f"review:{role}" + (" (same-vendor fallback)" if same_vendor else "")
    # The frozen manifest, not just the digest: the digest only moves when a new delivery is recorded, so without the
    # content hashes an artifact edited under the reviewer would still collect an ACCEPT for bytes that are gone
    #. loop.dispatch writes it next to the run as frozen_inputs.json for the audit.
    return supervisor.dispatch(review_task, adapter_name, note=note, kind="review",
                              extra={"review_role": role, "review_digest": digest, "same_vendor": same_vendor,
                                     "review_frozen": frozen(layout, artifacts)})


def rebuild(layout, rec: dict) -> Task | None:
    """Reconstruct the review-dispatch task view for a continuation (capacity/handoff requeue mid-review). None
    when the inputs moved on in the meantime — the caller drops the continuation and `pending()` queues a fresh
    review against the new digest instead."""
    from . import packets as packets_mod
    task = tasks_mod.load(layout, rec["task_id"])
    role, digest = rec.get("review_role"), rec.get("review_digest")
    if not role or task.delivery.digest != digest:
        return None
    adapter_name = (rec.get("spec") or {}).get("adapter", "codex_cli")
    artifacts = task.paths or task.artifacts
    brief = packets_mod.review_brief(layout, task, digest, artifacts, role)
    return _review_task_view(layout, task, role, adapter_name, brief, {})


def ingest(supervisor, rec: dict, result) -> None:
    """Turn a finished review job into a ReviewRecord (or discard it if its frozen inputs are stale), then let
    tasks.advance() decide the task's next status. A REVISE/REJECT verdict hands off to escalation once every
    required role has reported for this digest. BLOCKED is an interruption, not a verdict on the work: it is
    re-dispatched (budgeted), never recorded as a ReviewRecord; the task is left for the owner only once the
    budget is exhausted."""
    from . import escalation as escalation_mod
    layout = supervisor.layout
    task_id = rec["task_id"]
    role, digest = rec.get("review_role"), rec.get("review_digest")
    task = tasks_mod.load(layout, task_id)
    if not role or task.delivery.digest != digest:
        tasks_mod.log(layout, "review_invalidated", "loop", task=task_id, role=role, digest=digest)
        return
    stale = rec.get("review_frozen")
    if stale and "_unhashable" not in stale:
        # Two independent drift checks, because a redispatch after the first one freezes whatever bytes are on
        # disk *right now* as the new baseline — comparing only against that baseline can never see bytes that
        # had already drifted before THIS dispatch even started. Comparing the
        # dispatch-time baseline's own digest against the delivery digest catches that case; comparing the
        # baseline against a fresh re-hash catches drift during this review's own flight (the original check).
        baseline_mismatch = digest_of(stale) != digest
        drifted_in_flight = frozen(layout, list(stale)) != stale
        if baseline_mismatch or drifted_in_flight:
            # the artifacts themselves changed without a new delivery to move the digest: the verdict covers bytes
            # that are no longer the delivered ones, so it is discarded and pending() queues a fresh review — at
            # most MAX_INVALIDATIONS times per digest; after that the task is BLOCKED for the owner.
            changed = [p for p in stale if frozen(layout, [p]).get(p) != stale.get(p)] if drifted_in_flight else list(stale)
            n = sum(1 for r in jobs_mod.for_task(layout, task_id)
                    if (r.get("spec") or {}).get("kind") == "review" and r.get("review_digest") == digest
                    and r.get("phase") == "DONE" and r.get("invalidated"))
            jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], invalidated=True)
            reason = "frozen inputs changed" if drifted_in_flight else "frozen inputs never matched the delivery digest"
            tasks_mod.log(layout, "review_invalidated", "loop", task=task_id, role=role, digest=digest,
                          reason=reason, changed=changed[:10], count=n + 1)
            if n + 1 >= MAX_INVALIDATIONS:
                tasks_mod.set_status(layout, task_id, TaskStatus.BLOCKED, actor="loop",
                                     note=f"review of digest {digest[:12]} invalidated {n + 1}x (inputs keep changing: {changed[:5]}); owner decision needed")
            return
    obj = result.raw or {}
    verdict = obj.get("verdict")
    if result.status == "FAILED" or not verdict:
        # A reviewer that crashed, timed out or returned no verdict: pending() would re-dispatch the same role next
        # iteration forever. Budget it like an interruption: the same `phase == "DONE"` guard as below
        # keeps this attempt out of its own count; at the cap the task is BLOCKED and an issue carries the evidence.
        n = sum(1 for r in jobs_mod.for_task(layout, task_id)
                if (r.get("spec") or {}).get("kind") == "review" and r.get("review_role") == role
                and r.get("review_digest") == digest and r.get("phase") == "DONE" and r.get("failed_review"))
        jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], failed_review=True)
        tasks_mod.log(layout, "review_attempt_failed", "loop", task=task_id, role=role, error=result.error, count=n + 1)
        if n + 1 >= MAX_INVALIDATIONS:
            detail = f"review of digest {digest[:12]} by {role} failed {n + 1}x without a verdict (last: {result.error or 'no verdict'})"
            tasks_mod.set_status(layout, task_id, TaskStatus.BLOCKED, actor="loop", note=detail + "; owner decision needed")
            sig = escalation_mod.signature(f"review_failure:{role}:{digest}")
            if escalation_mod.find_issue(layout, task_id, sig) is None:
                escalation_mod.open_issue(layout, title=f"{task_id}: {role} review keeps failing", severity="high",
                                          owner_role=task.owner_role, detail=detail, resolution_task=task_id, signature=sig)
        return
    if verdict == "BLOCKED":
        # An interruption, not a verdict on the work (continuity protocol: a read-only reviewer that gets cut off
        # returns BLOCKED). Recording it as a ReviewRecord would be a one-way door: pending() never re-dispatches a
        # role with a live review for the digest, so the required verdict could never be obtained again by
        # automation. Re-dispatch instead, budgeted the same way as an
        # invalidation; only BLOCK the task for the owner once the cap is reached.
        # `phase == "DONE"` (same guard the invalidation branch above already uses) excludes the current attempt
        # from its own count: loop.ingest() only marks this attempt DONE *after* this function returns, so a crash
        # replayed before that point would otherwise find its own already-persisted blocked_review=True flag and
        # count itself twice — one real interruption charging the budget as two.
        n = sum(1 for r in jobs_mod.for_task(layout, task_id)
                if (r.get("spec") or {}).get("kind") == "review" and r.get("review_role") == role
                and r.get("review_digest") == digest and r.get("phase") == "DONE" and r.get("blocked_review"))
        jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], blocked_review=True)
        tasks_mod.log(layout, "review_interrupted", "loop", task=task_id, role=role, digest=digest,
                      summary=str(obj.get("summary") or "")[:300], count=n + 1)
        if n + 1 >= MAX_INVALIDATIONS:
            tasks_mod.set_status(layout, task_id, TaskStatus.BLOCKED, actor="loop",
                                 note=f"review of digest {digest[:12]} by {role} interrupted {n + 1}x; owner decision needed")
        return
    adapter_name = (rec.get("spec") or {}).get("adapter")
    vendor = VENDOR_OF_ADAPTER.get(adapter_name, adapter_name)
    run_dir = (rec.get("spec") or {}).get("run_dir")
    report = str((Path(run_dir) / "result.json").relative_to(layout.root).as_posix()) if run_dir else None
    try:
        tasks_mod.record_review(layout, task_id, digest, role, verdict, vendor=vendor, report=report, actor="loop")
    except tasks_mod.TaskError:
        # the digest moved on in the instant between our check above and the lock-protected write: discard, same
        # as the ordinary invalidation path above.
        tasks_mod.log(layout, "review_invalidated", "loop", task=task_id, role=role, digest=digest)
        return
    task = tasks_mod.advance(layout, task_id, actor="advance")
    # Escalate once per digest, after every required role has reported — never gated on the verdict that happens to
    # land last. A round whose last-landing verdict is ACCEPT (while an earlier required role already said REVISE)
    # used to return here before the round-completion check below ever ran, dropping the revision silently: no
    # record_failure, no revision brief, no issue, no task note, and nothing left WAITING_FOR_REVIEW for pending()
    # to re-dispatch. Two reviewers each failing the same digest used to call
    # record_failure independently too: the first dispatched a revision, and the second's call still charged a
    # signature and a budget slot before dispatch_revision's one-open-attempt guard refused to spend it, silently
    # dropping that reviewer's findings and starving the senior-model revision the budget was supposed to buy
    #.
    live = {r.role: r for r in task.delivery.reviews if r.digest == digest and not r.superseded}
    if not all(required in live for required in task.reviewer):
        tasks_mod.log(layout, "review_round_pending", "loop", task=task_id, role=role, verdict=verdict,
                      awaiting=[r for r in task.reviewer if r not in live])
        return
    bad = [r for r in live.values() if r.verdict in (ReviewVerdict.REVISE, ReviewVerdict.REJECT)]
    if not bad:
        return                                             # the round completed clean; advance() already applied it
    combined = "\n\n".join(f"{r.role} ({r.verdict.value}): {_findings_text(layout, r.report) or r.role + ' verdict ' + r.verdict.value}"
                           for r in bad)
    escalation_mod.record_failure(supervisor, task_id, "review", combined, event=rec["attempt_id"])


def _findings_text(layout, report_path: str | None) -> str:
    """Findings (or the summary) of a recorded review's own result.json, verbatim — reused so a revision brief
    quotes every required reviewer's evidence, not just the one that happened to complete the round."""
    if not report_path:
        return ""
    try:
        obj = read_json(layout.root / report_path)
    except (OSError, ValueError):
        return ""
    findings = "; ".join(f"[{f.get('category')}] {f.get('message')} ({f.get('evidence')})"
                         for f in obj.get("findings") or [])
    return findings or str(obj.get("summary") or "")


def salvage(supervisor, task: Task, role: str) -> bool:
    """Before paying for a fresh review: a finished review attempt for this digest and role whose answer was thrown
    away as unreadable (no result.json) may parse now that the contract accepts category near-misses (a reviewer
    that wrote "acceptance" for "requirement" lost a whole REVISE verdict that way). The newest readable answer is
    ingested exactly as if it had parsed the first time; True when one was."""
    import json as _json
    from ._vendor.contract import atomic_json, structured_result
    from .adapters.base import Result
    from .adapters.claude_cli import classify_status
    layout = supervisor.layout
    digest = task.delivery.digest
    recs = [r for r in jobs_mod.for_task(layout, task.id)
            if (r.get("spec") or {}).get("kind") == "review" and r.get("review_role") == role
            and r.get("review_digest") == digest and r.get("phase") in ("DONE", "FAILED") and not r.get("salvaged")]
    import re as _re
    recs.sort(key=lambda r: int((_re.search(r"(\d+)$", str(r["attempt_id"])) or [0, 0])[1]), reverse=True)   # newest first
    for r in recs:
        run_dir = Path((r.get("spec") or {}).get("run_dir") or "")
        if not str(run_dir) or not run_dir.is_dir() or (run_dir / "result.json").exists():
            continue
        texts = []
        follow = run_dir / "followup_stdout.jsonl"
        if follow.exists():
            for line in reversed(follow.read_text(encoding="utf-8-sig", errors="replace").splitlines()):
                try:
                    e = _json.loads(line)
                except ValueError:
                    continue
                if e.get("type") == "result":
                    texts.append(e.get("result") or "")
                    break
        if (run_dir / "result.txt").exists():
            texts.append((run_dir / "result.txt").read_text(encoding="utf-8", errors="replace"))
        for text in texts:
            try:
                obj = structured_result(text, "review")
            except ValueError:
                continue
            atomic_json(run_dir / "result.json", obj)
            rec = jobs_mod.set_phase(layout, r["attempt_id"], r["phase"], salvaged=True)
            tasks_mod.log(layout, "review_salvaged", "loop", task=task.id, role=role, attempt=r["attempt_id"],
                          verdict=obj.get("verdict"))
            ingest(supervisor, rec, Result(status=classify_status(obj), raw=obj, notes=str(obj.get("summary") or "")))
            return True
    return False
