"""Discovery & certificate-scan endpoints.

نقاط پایانی کشف سایت و اسکن گواهی.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.services import discovery, certs
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


@router.get("/vhosts")
async def vhosts(user: dict = Depends(get_current_user)) -> dict:
    return discovery.discover_vhosts()


@router.get("/health")
async def health_all(user: dict = Depends(get_current_user)) -> dict:
    return discovery.health_check_all()


@router.get("/health/{domain}")
async def health_one(domain: str, user: dict = Depends(get_current_user)) -> dict:
    return discovery.health_check(domain)


@router.get("/certificates")
async def cert_scan(user: dict = Depends(get_current_user)) -> dict:
    return certs.scan_server()
