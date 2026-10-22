"""Automatic local access with Host/Origin and invisible form protection."""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

from ..paths import Layout

CSRF_COOKIE = "supervisor_csrf"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _host_ok(host: str) -> bool:
    return host.split(":")[0] in ALLOWED_HOSTS


def check_host_origin(request: Request) -> None:
    """Defense in depth even though the server only binds loopback: a misconfigured proxy or a DNS-rebinding attempt
    must not be trusted just because the socket is local."""
    host = request.headers.get("host", "")
    if not host or not _host_ok(host):
        raise HTTPException(status_code=400, detail="bad Host header")
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        for header in ("origin", "referer"):
            value = request.headers.get(header)
            if value:
                stripped = value.split("://", 1)[-1]
                if not _host_ok(stripped.split("/")[0]):
                    raise HTTPException(status_code=400, detail=f"bad {header}")


def get_layout(request: Request) -> Layout:
    override = getattr(request.app.state, "layout_override", None)
    return override or Layout().ensure()


def require_auth(request: Request) -> Layout:
    """Local page and action dependency; no sign-in is required. Returns the Layout so handlers do not re-derive it."""
    check_host_origin(request)
    return get_layout(request)


def require_csrf(request: Request, form_token: str | None) -> None:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not form_token or not secrets.compare_digest(cookie, form_token):
        raise HTTPException(status_code=403, detail="missing or invalid csrf token")
