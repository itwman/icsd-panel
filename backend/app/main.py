"""ICSD Panel — FastAPI application entry point.

ورودی برنامهٔ FastAPI پنل ICSD.
Run (dev):  uvicorn app.main:app --reload --port 8088
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import settings
from app import db
from app.services import scheduler
from app.services import users
from app.api import (monitoring, system, auth, sites, databases, ssl, backup,
                     apps, discovery, security, email, files, notifications,
                     logs, setup, ftp, cron, webmail, pyapp)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("icsd")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    db.init_db()
    created = users.ensure_default_admin()
    if created:
        log.warning("کاربر پیش‌فرض ساخته شد / default admin created: %s / %s — %s",
                    created["username"], created["password"], created["warning"])
    scheduler.start()
    yield
    # Shutdown
    scheduler.shutdown()


app = FastAPI(
    title="ICSD Panel API",
    description="Open-source Linux server management panel — ICSD",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.debug else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (monitoring, system, auth, sites, databases, ssl, backup,
          apps, discovery, security, email, files, notifications, logs, setup,
          ftp, cron, webmail, pyapp):
    app.include_router(r.router)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": __version__}


# --- Frontend (single-file UI for Phase 0/1; Vue build replaces it in Phase 2) ---
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@app.get("/", response_model=None)
async def index() -> FileResponse | JSONResponse:
    page = _FRONTEND_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"message": "ICSD Panel API is running. See /docs"})


if _FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")
