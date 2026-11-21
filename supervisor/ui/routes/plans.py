"""Recovery plans page (RECOVERY_PLAN.md section 2.6): every state/plans/*.yaml document, its status, its journal
(`done`/`order`) and its history — read-only. Not yet linked from the main nav or offering a button: the typed
choices that let an owner apply_plan/cancel_plan from here (RECOVERY_PLAN.md section 4, steps 7-8) are not built
yet; until then the plan is cancelled or resumed with `supervisor plan cancel`/`plan apply`."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ... import plans as plans_mod
from ...paths import Layout
from ..auth import require_auth
from ..deps import decode_flash, render

router = APIRouter()


@router.get("/plans")
def plans_page(request: Request, layout: Layout = Depends(require_auth)):
    rows = sorted(plans_mod.all_plans(layout), key=lambda d: d.get("id", ""))
    return render(request, "plans.html", {"plans": rows, "flash": decode_flash(request)})


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: str, request: Request, layout: Layout = Depends(require_auth)):
    doc = plans_mod.load_plan(layout, plan_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"no such plan {plan_id!r}")
    ops = doc.get("operations") or []
    done = set(doc.get("done") or [])
    rows = [{"index": i, "op": op.get("op"), "done": i in done, "detail": op} for i, op in enumerate(ops)]
    return render(request, "plan_detail.html", {"plan": doc, "rows": rows, "flash": decode_flash(request)})
