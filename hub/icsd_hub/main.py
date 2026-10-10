"""ICSD Panel hub — the FastAPI application.

Run:  uvicorn icsd_hub.main:app --host 0.0.0.0 --port 8088 --proxy-headers
(one worker: live state lives in this process's memory)
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from icsd_hub import __version__, agents, alerts, api, auth, checks, config, db, live, metrics

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("icsd")

WEB = Path(__file__).parent / "web"
INSTALL_SH = Path(__file__).parent / "install-agent.sh"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


async def _ticker() -> None:
    """Background chores. Each is cheap; none may kill the loop."""
    tick = 0
    async with httpx.AsyncClient(headers={"User-Agent": f"ICSD-Panel/{__version__} (uptime check)"},
                                 http2=False) as client:
        while True:
            try:
                alerts.evaluate_offline()
                await checks.run_due(client)
                if tick % 40 == 0:      # ~10 minutes
                    metrics.rollup()
                    auth.prune()
                if tick % 240 == 1:     # ~hourly
                    metrics.prune()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("background tick failed")
            tick += 1
            await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    alerts.seed()
    if not db.one("SELECT 1 FROM users LIMIT 1"):
        log.warning("No users yet. Create the first admin:  docker compose exec hub icsd-hub create-admin")
    task = asyncio.create_task(_ticker())
    yield
    task.cancel()
    live.flush_all()
    db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="ICSD Panel", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    app.include_router(agents.router)
    app.include_router(api.router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": __version__}

    # ── agent distribution ─────────────────────────────────────────────
    @app.get("/install.sh")
    async def install_script(request: Request) -> PlainTextResponse:
        script = INSTALL_SH.read_text().replace("__HUB_URL__", agents.base_url(request))
        return PlainTextResponse(script, media_type="text/x-shellscript")

    @app.get("/dl/{name}")
    async def download(name: str) -> FileResponse:
        if not re.fullmatch(r"(icsd-agent-linux-(amd64|arm64)|SHA256SUMS|VERSION)", name):
            raise HTTPException(404)
        path = config.settings.dist_dir / name
        if not path.is_file():
            raise HTTPException(404, "agent binaries are not built into this hub image")
        return FileResponse(path, media_type="application/octet-stream")

    # ── the web UI (hash routing: one index.html) ──────────────────────
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")
    return app


app = create_app()
