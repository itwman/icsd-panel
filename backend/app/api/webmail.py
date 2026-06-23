"""Webmail (Roundcube) endpoints.

نقاط پایانی وب‌میل: پیش‌نمایش و نصب Roundcube روی یک دامنه.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services import webmail as webmail_service
from app.services.webmail import WebmailError
from app.services.sites import SiteError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/webmail", tags=["webmail"])


class WebmailIn(BaseModel):
    domain: str
    php_version: str = "8.2"
    imap_host: str = "localhost"
    smtp_host: str = "localhost"
    dry: bool = False


@router.get("/info")
async def info(user: dict = Depends(get_current_user)) -> dict:
    return {"app": "roundcube", "download": webmail_service.ROUNDCUBE_URL,
            "note": "Roundcube به سرور IMAP/SMTP محلی وصل می‌شود؛ ابتدا بخش ایمیل را راه‌اندازی کنید."}


@router.post("")
async def install(body: WebmailIn, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return webmail_service.install(
            body.domain, php_version=body.php_version,
            imap_host=body.imap_host, smtp_host=body.smtp_host, apply=not body.dry)
    except (WebmailError, SiteError) as e:
        raise HTTPException(status_code=400, detail=str(e))
