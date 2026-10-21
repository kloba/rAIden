"""The FastAPI application: API routers, the web UI (single-page app) and the background runtime."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import re
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .runtime import Runtime
from .settings import ConsoleSettings

log = logging.getLogger("raiden.console")


def _static_dir(settings: ConsoleSettings) -> Path | None:
    candidates = [settings.static_dir] if settings.static_dir else []
    here = Path(__file__).resolve()
    candidates += [here.parent / "static", here.parents[2] / "web" / "dist"]
    for c in candidates:
        if c and (c / "index.html").is_file():
            return c
    return None


def content_security_policy(index: Path | None) -> str:
    """Scripts only from the console itself, plus the one inline script of index.html (it sets the theme before the first
    paint), allowed by its hash; no frames, no plugins, no other origins."""
    hashes = []
    if index is not None:
        for body in re.findall(r"<script>(.*?)</script>", index.read_text(), re.S):
            hashes.append("'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'")
    return ("default-src 'self'; script-src 'self' " + " ".join(hashes) + "; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


class SecurityHeaders:
    """Headers on every response: no sniffing, no framing, no referrer to other sites, nothing cached from the API, the
    content security policy for the UI (not for /api/docs, which loads its assets from a CDN) and HSTS over HTTPS."""

    def __init__(self, app: Any, csp: str, hsts: bool) -> None:
        self.app, self.csp, self.hsts = app, csp, hsts

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("X-Frame-Options", "DENY")
                h.setdefault("Referrer-Policy", "same-origin")
                if path.startswith("/api/") and not path.startswith("/api/docs"):
                    h.setdefault("Cache-Control", "no-store")
                if not path.startswith("/api/docs"):
                    h.setdefault("Content-Security-Policy", self.csp)
                if self.hsts:
                    h.setdefault("Strict-Transport-Security", "max-age=31536000")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(settings: ConsoleSettings | None = None, start_background: bool = True) -> FastAPI:
    settings = settings or ConsoleSettings.from_env()
    runtime = Runtime(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.bootstrap()
        if start_background:
            await runtime.start()
        else:
            import asyncio

            runtime.bus.attach(asyncio.get_running_loop())
        log.info("rAIden Console %s ready (%s, %s)", __version__, "demo fleet" if settings.demo else "live", "SQLite" if settings.is_sqlite else "PostgreSQL")
        yield
        await runtime.stop()

    app = FastAPI(title="rAIden Console", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None,
                  description="The API of rAIden Console, the AI on-call DBA for PostgreSQL fleets: servers and their metrics, incidents and "
                              "approvals, the autonomy policy, monitors, routines, chat, what rAIden learned (knowledge, learned approvals, "
                              "dreams), AI endpoints, people and the audit log. Sign in with POST /api/v1/auth/login; every change needs the "
                              "header X-Requested-With: raiden.")
    app.state.rt = runtime

    from .api import auth, estate, incidents, knowledge, rest

    for r in (auth.router, estate.router, incidents.router, knowledge.router, rest.router):
        app.include_router(r)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            with runtime.db.session() as s:
                s.execute(__import__("sqlalchemy").text("SELECT 1"))
            return JSONResponse({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=503)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    static = _static_dir(settings)
    app.add_middleware(SecurityHeaders, csp=content_security_policy(static / "index.html" if static else None), hsts=settings.cookie_secure)
    if static is not None:
        index = static / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise StarletteHTTPException(404, "Not found.")
            target = (static / path).resolve()
            if path and target.is_file() and static.resolve() in target.parents:
                headers = {"Cache-Control": "public, max-age=31536000, immutable"} if path.startswith("assets/") else {}
                return FileResponse(target, headers=headers)
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
    else:
        @app.get("/", include_in_schema=False)
        def no_ui() -> JSONResponse:
            return JSONResponse({"detail": "The web UI is not built. Run `npm ci && npm run build` in web/ (the API is at /api/docs)."}, status_code=503)

    return app
