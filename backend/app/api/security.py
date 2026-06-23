"""Security endpoints — IDS, IP monitoring, malware scan.

نقاط پایانی امنیت.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services import security_scan as sec
from app.services.security_scan import SecurityError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/security", tags=["security"])


@router.get("/fail2ban")
async def fail2ban(user: dict = Depends(get_current_user)) -> dict:
    return sec.fail2ban_status()


@router.get("/connections")
async def connections(user: dict = Depends(get_current_user)) -> dict:
    return sec.active_connections()


@router.get("/failed-logins")
async def failed_logins(user: dict = Depends(get_current_user)) -> dict:
    return sec.failed_logins()


class BanRequest(BaseModel):
    jail: str
    ip: str


@router.post("/ban")
async def ban(body: BanRequest, user: dict = Depends(require_role("admin"))) -> dict:
    try:
        return sec.ban_ip(body.jail, body.ip)
    except SecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/unban")
async def unban(body: BanRequest, user: dict = Depends(require_role("admin"))) -> dict:
    try:
        return sec.unban_ip(body.jail, body.ip)
    except SecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ScanRequest(BaseModel):
    path: str


@router.post("/scan")
async def scan(body: ScanRequest, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return sec.scan_path(body.path)
    except SecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/scan-sites")
async def scan_sites(user: dict = Depends(require_role("manager"))) -> dict:
    return sec.scan_all_sites()
