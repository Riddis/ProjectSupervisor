"""Shared Jinja environment, flash-message encoding and the command-submission helper every action route uses."""
from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import commands as commands_mod
from ..paths import Layout
from .assets import stylesheet_url
from .auth import CSRF_COOKIE

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["now"] = lambda: time.time()
templates.env.globals["stylesheet_url"] = stylesheet_url


def _inbox_badge(request: Request) -> int:
    from .. import inbox as inbox_mod
    from . import auth as auth_mod
    return len(inbox_mod.open_items(auth_mod.get_layout(request)))


templates.env.globals["inbox_badge"] = _inbox_badge


def _project_name(request: Request) -> str:
    from .. import project as project_mod
    from . import auth as auth_mod
    try:
        return project_mod.name(auth_mod.get_layout(request))
    except Exception:                                       # noqa: BLE001 - a template must still render
        return "Project"


def _extension_nav(request: Request) -> list[tuple[str, str]]:
    from .. import extensions as extensions_mod
    from . import auth as auth_mod
    try:
        return [(path, label) for _router, label, path in extensions_mod.load(auth_mod.get_layout(request)).ui_routers]
    except Exception:                                       # noqa: BLE001
        return []


templates.env.globals["extension_nav"] = _extension_nav


def render(request: Request, name: str, context: dict):
    context = {"csrf_token": getattr(request.state, "csrf_token", request.cookies.get(CSRF_COOKIE, "")),
               "project_name": _project_name(request), **context}
    return templates.TemplateResponse(request, name, context)


def encode_flash(level: str, message: str) -> str:
    return urllib.parse.quote(f"{level}:{message}")


def decode_flash(request: Request) -> dict | None:
    raw = request.query_params.get("flash")
    if not raw:
        return None
    raw = urllib.parse.unquote(raw)
    level, _, message = raw.partition(":")
    return {"level": level, "message": message}


def act(request: Request, layout: Layout, kind: str, payload: dict, redirect_to: str) -> RedirectResponse:
    """Submit a command and report the outcome. HTMX requests get a rendered fragment; plain form posts (and tests
    that don't send HX-Request) get a redirect back to the page with a flash banner describing what happened."""
    outcome = commands_mod.submit_or_apply(layout, kind, payload, actor="owner-ui")
    if outcome.get("queued"):
        level, message = "pending", f"{kind} queued ({outcome['id']}) — applied by the running supervisor"
    elif outcome.get("ok", True):
        level, message = "applied", f"{kind} applied"
    else:
        level, message = "error", outcome.get("error", "failed")
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/action_result.html",
                                          {"level": level, "message": message, "outcome": outcome})
    return RedirectResponse(url=f"{redirect_to}?flash={encode_flash(level, message)}", status_code=303)
