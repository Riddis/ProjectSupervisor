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


ACTIVE = {TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.WAITING_FOR_REVIEW, TaskStatus.WAITING_FOR_HUMAN,
          TaskStatus.BLOCKED}
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
    TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.COMPLETE: set(),
    TaskStatus.CANCELLED: {TaskStatus.NOT_STARTED},
}


class HumanApproval(str, Enum):
    none = "none"
    required = "required"
    gate = "gate"


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
    id: str
    resource: str
    at: str
    outcome: str = ""
    note: str = ""
    model: Optional[str] = None
    session_id: Optional[str] = None
    run_dir: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)

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

    model_config = {"extra": "forbid"}


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
    checks: list[Check] = Field(default_factory=list)           # executable: what the supervisor re-runs
    reviewer: list[str] = Field(default_factory=list)
    review_note: Optional[str] = None
    acceptance: str = ""
    escalation: str = "none"
    human_approval: HumanApproval = HumanApproval.none
    approval_note: Optional[str] = None
    cost_category: CostCategory = CostCategory.claude
    status: TaskStatus = TaskStatus.NOT_STARTED
    attempts: list[Attempt] = Field(default_factory=list)      # dispatch attempts only (retry budgets, §2.6)
    history: list[dict[str, Any]] = Field(default_factory=list)  # status changes {at, by, from, to, note, forced?}
    forced: Optional[dict[str, Any]] = None                     # set when the owner forced a status; rendered
    result: Optional[str] = None
    paths: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    owned_paths: list[str] = Field(default_factory=list)
    priority: int = 0
    delivery: Delivery = Field(default_factory=Delivery)
    created: Optional[str] = None
    updated: Optional[str] = None
    rev: int = 0

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

    def needs_review(self) -> bool:
        return bool(self.reviewer)

    def needs_approval(self) -> bool:
        return self.human_approval != HumanApproval.none
