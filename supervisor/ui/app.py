"""`supervisor ui`: FastAPI + Uvicorn Control Center on 127.0.0.1:8790 by default (project.yaml ui.port). The UI implements no policy — every
mutating route funnels through supervisor.commands, the same library the CLI uses."""
from __future__ import annotations

import os
import secrets
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import plugins as plugins_mod
from ..paths import Layout
from . import live
from .assets import STATIC_DIR, stylesheet
from .auth import CSRF_COOKIE, check_host_origin, get_layout
from .routes import router as routes_router

DEFAULT_PORT = 8790


UPDATE_CHECK_EVERY_S = 1800


def _start_update_checker(layout: Layout) -> None:
    """Fetch origin in the background so the overview banner is current without a fetch inside a page render."""
    import threading
    from .. import updates

    def loop():
        while True:
            try:
                updates.check(layout, fetch=True, max_age_s=0)
            except Exception:                               # noqa: BLE001 - the banner just stays stale
                pass
            time.sleep(UPDATE_CHECK_EVERY_S)
    threading.Thread(target=loop, name="update-check", daemon=True).start()


def create_app(layout: Layout | None = None, update_check: bool | None = None) -> FastAPI:
    """`update_check`: run the background origin fetch (default: yes, unless SUPERVISOR_UPDATE_CHECK=0 — tests and
    offline machines must never have a page render depend on, or a test suite spawn, a network fetch)."""
    if update_check is None:
        update_check = os.environ.get("SUPERVISOR_UPDATE_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")
    app = FastAPI(title="Control Center", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.layout_override = layout

    @app.middleware("http")
    async def _host_origin_guard(request: Request, call_next):
        try:
            check_host_origin(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        request.state.csrf_token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)
        if request.method == "GET" and not live.skip_stamp(request.url.path):
            # Taken before the route reads any state: a page is stamped with a fingerprint no newer than its data,
            # so a change landing mid-render makes the open page refresh once more instead of never.
            try:
                request.state.live_version = live.fingerprint(get_layout(request))
            except Exception:                               # noqa: BLE001 - the page renders; live.js just refreshes
                request.state.live_version = ""
        response = await call_next(request)
        if "text/html" in response.headers.get("content-type", ""):
            response.headers["Cache-Control"] = "no-store"
        if not request.cookies.get(CSRF_COOKIE):
            response.set_cookie(CSRF_COOKIE, request.state.csrf_token, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/assets/control-center-{version}.css", include_in_schema=False)
    def control_center_styles(version: str):
        current_version, content = stylesheet()
        if version != current_version:
            raise HTTPException(status_code=404, detail="stylesheet changed: reload the page")
        # Do not let Last-Modified/ETag checks reuse an older response during local edits.
        return Response(content, media_type="text/css", headers={"Cache-Control": "no-store"})

    reg = plugins_mod.load(layout) if layout is not None else None
    if reg is not None:
        # Plugin static files first: Starlette tries mounts in order, and `/static/<prefix>` must win over `/static`.
        for prefix, directory in reg.static_dirs:
            app.mount(f"/static/{prefix}", StaticFiles(directory=str(directory)), name=f"static-{prefix}")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(routes_router)
    if reg is not None:
        from .deps import use_plugin_templates
        use_plugin_templates(reg.template_dirs)      # plugin pages may extend base.html (registry.template_dir)
        if update_check:
            _start_update_checker(layout)
        for ext_router, _label, _path in reg.ui_routers:
            app.include_router(ext_router)
    return app


app = create_app()


def run(layout: Layout | None = None, host: str = "127.0.0.1", port: int = DEFAULT_PORT, reload: bool = False) -> None:
    import uvicorn

    layout = (layout or Layout()).ensure()
    print(f"Control Center: http://{host}:{port}/ (local access, no sign-in)")
    uvicorn.run(create_app(layout), host=host, port=port, log_level="info")
