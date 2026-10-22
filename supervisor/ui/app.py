"""`supervisor ui`: FastAPI + Uvicorn Control Center on 127.0.0.1:8790 by default (project.yaml ui.port). The UI implements no policy — every
mutating route funnels through supervisor.commands, the same library the CLI uses."""
from __future__ import annotations

import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import extensions as extensions_mod
from ..paths import Layout
from .assets import STATIC_DIR, stylesheet
from .auth import CSRF_COOKIE, check_host_origin
from .routes import router as routes_router

DEFAULT_PORT = 8790


def create_app(layout: Layout | None = None) -> FastAPI:
    app = FastAPI(title="Control Center", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.layout_override = layout

    @app.middleware("http")
    async def _host_origin_guard(request: Request, call_next):
        try:
            check_host_origin(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        request.state.csrf_token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)
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

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(routes_router)
    if layout is not None:
        for ext_router, _label, _path in extensions_mod.load(layout).ui_routers:
            app.include_router(ext_router)
    return app


app = create_app()


def run(layout: Layout | None = None, host: str = "127.0.0.1", port: int = DEFAULT_PORT, reload: bool = False) -> None:
    import uvicorn

    layout = (layout or Layout()).ensure()
    print(f"Control Center: http://{host}:{port}/ (local access, no sign-in)")
    uvicorn.run(create_app(layout), host=host, port=port, log_level="info")
