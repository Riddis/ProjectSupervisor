"""Issues page: every state/issues/*.yaml record, with a Resolve action (R-21) queued like every other command
(issue.resolve) — this module never writes state/issues/ itself."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ... import store
from ...paths import Layout
from ..auth import require_auth, require_csrf
from ..deps import act, decode_flash, render

router = APIRouter()


def _issues(layout: Layout) -> list[dict]:
    return [store.read(p) for p in sorted(layout.issues.glob("I-*.yaml"), reverse=True)]


@router.get("/issues")
def issues_page(request: Request, layout: Layout = Depends(require_auth)):
    from ... import render as render_mod

    rows = _issues(layout)
    return render(request, "issues.html", {"issues": rows, "counts": render_mod.issue_counts(layout),
                                           "flash": decode_flash(request)})


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(issue_id: str, request: Request, resolution: str = Form(""), csrf_token: str = Form(...),
                  layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "issue.resolve", {"issue_id": issue_id, "resolution": resolution,
                                                  "resolving_task": None}, "/issues")
