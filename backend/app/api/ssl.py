"""SSL / Let's Encrypt endpoints.

نقاط پایانی SSL / لتس‌انکریپت.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services import ssl as ssl_service
from app.services.ssl import SSLError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/ssl", tags=["ssl"])


class IssueRequest(BaseModel):
    domain: str
    method: str = "dns"            # dns | http
    dns_provider: str | None = None
    cas: list[str] | None = None
    webroot: str | None = None
    apply: bool = True


@router.get("")
async def list_certs(user: dict = Depends(get_current_user)) -> dict:
    return ssl_service.list_certificates()


@router.get("/status")
async def acme_status(user: dict = Depends(get_current_user)) -> dict:
    return {"acme_installed": ssl_service.acme_installed(), "ca_fallback": ssl_service.CA_FALLBACK}


@router.post("/issue")
async def issue(body: IssueRequest, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return ssl_service.issue_certificate(
            body.domain, method=body.method, dns_provider=body.dns_provider,
            cas=body.cas, webroot=body.webroot, apply=body.apply,
        )
    except SSLError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/renew")
async def renew(user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return ssl_service.renew_all(apply=True)
    except SSLError as e:
        raise HTTPException(status_code=400, detail=str(e))
