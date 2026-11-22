"""Task contract and delivery records."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class TaskStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    BLOCKED = "BLOCKED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {TaskStatus.COMPLETE, TaskStatus.CANCELLED}

# Manual/loop transitions (advance() handles delivery-driven ones separately).
ALLOWED = {
    TaskStatus.NOT_STARTED: {TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
    TaskStatus.READY: {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED, TaskStatus.NOT_STARTED},
    TaskStatus.IN_PROGRESS: {TaskStatus.WAITING_FOR_REVIEW, TaskStatus.WAITING_FOR_HUMAN, TaskStatus.BLOCKED,
                             TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETE},
    TaskStatus.WAITING_FOR_REVIEW: {TaskStatus.IN_PROGRESS, TaskStatus.WAITING_FOR_HUMAN, TaskStatus.BLOCKED,
                                    TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETE},
    TaskStatus.WAITING_FOR_HUMAN: {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED,
                                   TaskStatus.COMPLETE, TaskStatus.FAILED},
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED, TaskStatus.FAILED},
    # BLOCKED is here so hold() can stop a queued retry at the moment it is most needed - just after a failure,
    # before the automatic retry races the owner's own repair (R-18).
    TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
    TaskStatus.COMPLETE: set(),
    TaskStatus.CANCELLED: {TaskStatus.NOT_STARTED},
}


class HumanApproval(str, Enum):
    none = "none"
    required = "required"
    gate = "gate"


class Evidence(str, Enum):
    """What a delivery must report to verify (G1, 2.4.0): `auto` keeps the pre-2.4.0 rule (a command is required
    exactly when the task declares `tests` and the job ran on a model lane); `commands` requires one on any lane,
    even with no declared `tests`; `review` requires none — the criteria are judged by reading the artifacts."""
    auto = "auto"
    commands = "commands"
    review = "review"


class CostCategory(str, Enum):
    """What a task costs when it runs; routing.yaml `cost_lanes` maps each to a default adapter."""
    deterministic = "deterministic"   # software only (python/process adapters)
    local = "local"                   # a local resource lane (a plugin adapter holding the resource lock)
    claude = "claude"
    codex = "codex"
    human = "human"
    mixed = "mixed"
    paid = "paid"                     # metered API credits (claude_api)


class Attempt(BaseModel):
    """ATTEMPT_MODEL.md §4.6 (step 15): `id`, `model`, `session_id` and `extra` are gone — nothing ever wrote the
    first three, and the fourth was never written either; a reader now takes the model from the job record
    (§2.7), never from this document. `at` is gone too, renamed to `opened` (closed sits beside it, §2.3's
    delivery-attempt shape). `model_config` stays `extra: allow` so a document written before this release —
    whose `attempts[]` entries still carry `id`/`at`/`model`/`session_id`/`extra` on disk — still loads; those
    bytes are tolerated, never read back through a declared field."""
    resource: Optional[str] = None      # per §4.6, moved onto the job entry in the migrated shape (`jobs[].resource`)
                                          # — optional so a document already on that shape still parses
    outcome: str = ""
    note: str = ""
    run_dir: Optional[str] = None

    # the delivery-attempt shape the migration writes (§2.7): additive here, unused until the writer switch
    n: Optional[int] = None
    opened: Optional[str] = None
    closed: Optional[str] = None
    cause: Optional[str] = None
    jobs: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class ReviewVerdict(str, Enum):
    ACCEPT = "ACCEPT"
    REVISE = "REVISE"
    REJECT = "REJECT"
    BLOCKED = "BLOCKED"


class ApprovalResponse(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    MODIFY = "MODIFY"
    REDO = "REDO"


class VerificationRecord(BaseModel):
    digest: str
    ok: bool
    at: str
    report: Optional[str] = None       # path to the verification report
    detail: str = ""


class ReviewRecord(BaseModel):
    digest: str
    role: str
    verdict: ReviewVerdict
    at: str
    vendor: Optional[str] = None
    report: Optional[str] = None
    report_sha256: Optional[str] = None    # the report's bytes when the review was recorded (1.10.0); the approval view
                                           # shows its summary/findings only while the file still hashes to this
    findings: list[dict[str, Any]] = Field(default_factory=list)   # R-15: the reviewer's own findings (with
                                                                     # findings[].id), kept on the task document
                                                                     # itself so a later review pass can read them
                                                                     # without ever reaching into a run directory
    superseded: bool = False


class ApprovalRecord(BaseModel):
    digest: str
    by: str                            # "owner" or an inbox item id
    response: ApprovalResponse         # only ACCEPT counts as approval
    at: str
    note: str = ""
    command_id: Optional[str] = None
    superseded: bool = False


class Delivery(BaseModel):
    """Everything advance() needs, all bound to the artifact digest of the current delivery."""
    digest: Optional[str] = None
    verification: Optional[VerificationRecord] = None
    reviews: list[ReviewRecord] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)


EXCLUSIVE = "."          # owned_paths: ["."] — "may touch anything, so nothing else may write meanwhile"


class Check(BaseModel):
    """An executable acceptance check fixed by the task contract. Verification always re-runs every
    check; a worker's reported `tests` are additional evidence and can never replace these."""
    command: str
    timeout_s: Optional[int] = None
    cwd: Optional[str] = None            # G2, 2.4.0: one of the task's own `test_roots`, or the project root

    model_config = {"extra": "forbid"}


class JobPreflight(BaseModel):
    """`supervisor.preflight`'s own record of one write-time check (RECOVERY_PLAN.md section 3.3): `ok` is the
    one `supervisor.gate` reads; `detail['block_digest']` is the job block's own content, `preflight` excluded,
    the moment this result was recorded -- a mismatch against the block now on disk means it changed since, and
    the gate refuses exactly as it would an absent or failed one."""
    ok: bool
    at: str
    template: Optional[str] = None
    probe_digest: Optional[str] = None
    detail: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class Job(BaseModel):
    """What a lane's `prepare()` reads (RECOVERY_PLAN.md section 3.2): the framework-known keys, typed — a wrong
    type is refused the moment a document carrying it is validated. An adapter's own keys beyond these (a
    plugin lane's `workflow`, `base_url`, ...) are not declared here, since this model does not know which
    adapter it belongs to; they are kept (`extra: allow`) and checked separately, against whatever schema the
    adapter declared through `registry.job_schema()` (`tasks.job_schema_problems`) — refused for a declared
    schema's unknown key, only ever warned for one with no schema at all, so a plugin from before this release
    keeps working unchanged."""
    adapter: Optional[str] = None
    template: Optional[str] = None                             # a plan's own writer: a reference, never a command
    params: dict[str, Any] = Field(default_factory=dict)
    argv: Optional[list[str]] = None                            # the owner's own writer only (RECOVERY_PLAN.md 3.4)
    fn: Optional[str] = None
    timeout_s: Optional[int] = None
    idle_timeout_s: Optional[int] = None
    retry_on: list[int] = Field(default_factory=list)          # exit codes worth one retry (loop.py's own check)
    stdin_file: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    prompt: Optional[str] = None
    result_kind: Optional[str] = None
    preflight: Optional[JobPreflight] = None                   # RECOVERY_PLAN.md section 3.3: written at the
                                                                 # same time as the rest of the block, never by hand

    model_config = {"extra": "allow"}


class Task(BaseModel):
    id: str
    phase: int
    title: str
    objective: str
    owner_role: str
    depends_on: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)              # prose: what must be true (for the worker and reviewers)
    evidence: Evidence = Evidence.auto                          # G1, 2.4.0: must a delivery report a command
    checks: list[Check] = Field(default_factory=list)           # executable: what the supervisor re-runs
    test_roots: list[str] = Field(default_factory=list)         # G2, 2.4.0: directories a declared command may `cwd` into
    reviewer: list[str] = Field(default_factory=list)
    review_note: Optional[str] = None
    acceptance: str = ""
    escalation: str = "none"
    human_approval: HumanApproval = HumanApproval.none
    approval_note: Optional[str] = None
    cost_category: CostCategory = CostCategory.claude
    status: TaskStatus = TaskStatus.NOT_STARTED
    attempts: list[Attempt] = Field(default_factory=list)      # dispatch attempts only (retry budgets, §2.6)
    job_seq: int = 0                                           # highest job number allocated (R-03); 0 on a task
                                                                 # written before this field existed
    counters: dict[str, Any] = Field(default_factory=dict)     # §2.3: {attempt, jobs: {<n>: {s,v,r,d}},
                                                                 # task_jobs: {<k>: n}} — tasks.allocate_job's own
                                                                 # block, monotonic, {} on a task with none yet

    history: list[dict[str, Any]] = Field(default_factory=list)  # status changes {at, by, from, to, note, forced?}
    forced: Optional[dict[str, Any]] = None                     # set when the owner forced a status; rendered
    held: Optional[dict[str, Any]] = None                       # an owner Hold's durable identity ({by, at, note});
                                                                 # set only by tasks.hold(), cleared only by the
                                                                 # owner's own later status change (round-4 F9) -
                                                                 # never inferred from status, history order or an
                                                                 # escalation event's `routed` flag
    job: Optional[Job] = None                                   # RECOVERY_PLAN.md 3.2/3.4: typed, under a writer rule
    result: Optional[str] = None
    findings_response: list[dict[str, Any]] = Field(default_factory=list)   # R-15, ATTEMPT_MODEL.md 4.3 point 3:
                                                                               # the producer's own optional, per-
                                                                               # finding reply {id, status, note?} to
                                                                               # the previous round's own findings —
                                                                               # additive beside `result`, the one
                                                                               # free-text account read when a
                                                                               # finding has none of these
    paths: list[str] = Field(default_factory=list)
    owned_paths: list[str] = Field(default_factory=list)
    priority: int = 0
    delivery: Delivery = Field(default_factory=Delivery)
    created: Optional[str] = None
    updated: Optional[str] = None
    rev: int = 0
    contract_rev: int = 1               # RECOVERY_PLAN.md section 5, D-027 decision 12: distinct from `rev`, which
                                          # `store.update` bumps on every write (a status change, an attempt entry,
                                          # a note); this moves only when `tasks._bump_contract_rev` sees a
                                          # CONTRACT_FIELDS value actually change — "did the brief move", not
                                          # "did this document move". The job record and the delivery brief state
                                          # the generation they were built from; a review recorded against an
                                          # older one is superseded once a judged-set field changes under it.

    model_config = {"extra": "allow", "use_enum_values": False}

    @field_validator("reviewer", mode="before")
    @classmethod
    def _reviewer_list(cls, v):
        if v is None or v == "none":
            return []
        if isinstance(v, str):
            raise ValueError("reviewer must be a list of role ids")
        return v

    @field_validator("depends_on", mode="before")
    @classmethod
    def _deps(cls, v):
        return v or []

    @field_validator("checks", mode="before")
    @classmethod
    def _checks(cls, v):
        if not v:
            return []
        return [{"command": c} if isinstance(c, str) else c for c in v]

    @field_validator("owned_paths", mode="before")
    @classmethod
    def _owned(cls, v):
        v = v or []
        return [EXCLUSIVE] if EXCLUSIVE in v else v           # exclusive mode subsumes any narrower root

    def exclusive(self) -> bool:
        """Exclusive ownership: the whole working tree, so no other writing job may run concurrently."""
        return EXCLUSIVE in self.owned_paths

    def scoped_paths(self) -> list[str]:
        """owned_paths narrower than the tree — the roots containment is checked against; [] in exclusive mode."""
        return [] if self.exclusive() else list(self.owned_paths)

    def requires_commands(self, model_lane: bool) -> bool:
        """Whether this delivery must report at least one re-run command (G1, 2.4.0)."""
        if self.evidence == Evidence.commands:
            return True
        if self.evidence == Evidence.review:
            return False
        return bool(model_lane and self.tests)          # "auto": the pre-2.4.0 rule, unchanged
