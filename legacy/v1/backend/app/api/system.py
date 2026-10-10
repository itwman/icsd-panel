"""System information endpoints — OS detection, service status.

نقاط پایانی اطلاعات سیستم: تشخیص توزیع و وضعیت سرویس‌ها.
"""
from __future__ import annotations

import shutil
import subprocess

from fastapi import APIRouter

from app.core.osdetect import detect_os
from app import __version__

router = APIRouter(prefix="/api/system", tags=["system"])

# Services we care about for the dashboard "service status" widget.
_WATCHED_SERVICES = ["nginx", "apache2", "httpd", "mysql", "mariadb", "ssh", "sshd"]


@router.get("/info")
async def system_info() -> dict:
    """Distribution info + panel version. Used by installer & dashboard header."""
    os_info = detect_os()
    return {"panel_version": __version__, "os": os_info.to_dict()}


def _service_active(name: str) -> bool | None:
    """Return True/False if systemctl knows the service, else None."""
    if not shutil.which("systemctl"):
        return None
    try:
        res = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip() == "active":
            return True
        # 'inactive'/'failed' = known but not running; 'unknown' = not installed
        if res.stdout.strip() in {"inactive", "failed", "activating", "deactivating"}:
            return False
        return None
    except (subprocess.SubprocessError, OSError):
        return None


@router.get("/services")
async def services_status() -> dict:
    """Status of common services for the dashboard."""
    statuses = []
    for svc in _WATCHED_SERVICES:
        state = _service_active(svc)
        if state is None:
            continue  # not installed on this host
        statuses.append({"name": svc, "active": state})
    return {"services": statuses}
