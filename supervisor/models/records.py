"""Decision, issue, inbox, gate and owner-task records."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class Decision(BaseModel):
    id: str
    date: str
    title: str
    status: str = "accepted"           # proposed | accepted | superseded
    decided_by: str = ""
    context: str = ""
    decision: str = ""
    consequences: str = ""
    supersedes: Optional[str] = None
    path: Optional[str] = None

    model_config = {"extra": "allow"}


class Issue(BaseModel):
    id: str
    created: str
    status: str = "open"               # open | resolved | wontfix
    severity: str = "medium"           # blocking | high | medium | low
    title: str
    owner_role: str = "manager"
    resolution_task: Optional[str] = None
    systemic: bool = False
    notes: list[str] = Field(default_factory=list)
    rev: int = 0

    model_config = {"extra": "allow"}


class InboxItem(BaseModel):
    id: str
    created: str
    kind: str = "question"             # gate | approval | round | spend | policy | question
    for_: str = Field(default="owner", alias="for")
    from_role: str = Field(default="manager", alias="from")
    status: str = "open"               # open | answered | snoozed | archived
    subject: str
    summary: str = ""
    task_id: Optional[str] = None
    digest: Optional[str] = None       # artifact digest the answer applies to
    allowed_responses: list[str] = Field(default_factory=lambda: ["ACCEPT", "REJECT", "MODIFY"])
    evidence: list[str] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)   # per-option sub-items
    answer: Optional[dict[str, Any]] = None
    answered_at: Optional[str] = None
    rev: int = 0

    model_config = {"extra": "allow", "populate_by_name": True}


class Gate(BaseModel):
    id: str
    phase: int
    status: str = "OPEN"               # OPEN | PASSED | FAILED
    evidence: list[str] = Field(default_factory=list)
    passed_at: Optional[str] = None
    owner_ack: Optional[str] = None

    model_config = {"extra": "allow"}


class OwnerTask(BaseModel):
    id: str
    title: str
    status: str = "open"
    blocks: list[str] = Field(default_factory=list)
    priority: str = "medium"
    guidance: str = ""

    model_config = {"extra": "allow"}
