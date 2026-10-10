"""Notification settings & alerts endpoints.

نقاط پایانی تنظیمات اعلان و هشدارها: خواندن/ذخیرهٔ پیکربندی، ارسال پیام آزمایشی،
و اجرای دستی بررسی هشدارها. رمز SMTP هرگز به UI برگردانده نمی‌شود.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services import notify as notify_service
from app.services.notify import NotifyError
from app.services import settings_store as ss
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# Settings that are safe to expose to the UI (everything except secrets).
_FIELDS = [
    "notify.telegram_enabled", "notify.telegram_chat_id",
    "notify.email_enabled", "notify.smtp_host", "notify.smtp_port",
    "notify.smtp_user", "notify.smtp_from", "notify.email_to", "notify.smtp_tls",
    "notify.ssl_days", "notify.disk_percent",
]
_SECRETS = ["notify.telegram_token", "notify.smtp_password"]


class NotifyConfig(BaseModel):
    telegram_enabled: bool | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    email_enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    email_to: str | None = None
    smtp_tls: bool | None = None
    ssl_days: int | None = None
    disk_percent: int | None = None


@router.get("/config")
async def get_config(user: dict = Depends(get_current_user)) -> dict:
    cfg = {k.replace("notify.", ""): ss.get(k) for k in _FIELDS}
    # report only whether secrets are set, never their values
    cfg["telegram_token_set"] = bool(ss.get("notify.telegram_token"))
    cfg["smtp_password_set"] = bool(ss.get("notify.smtp_password"))
    return {"config": cfg}


@router.post("/config")
async def save_config(body: NotifyConfig, user: dict = Depends(require_role("manager"))) -> dict:
    data = body.model_dump(exclude_unset=True)
    out = {}
    for key, val in data.items():
        if val is None:
            continue
        if key in ("telegram_token", "smtp_password") and val == "":
            continue  # empty secret = keep existing
        out[f"notify.{key}"] = val
    if out:
        ss.set_many(out)
    return {"saved": True, "keys": list(out.keys())}


@router.post("/test")
async def send_test(user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return notify_service.test()
    except NotifyError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/check")
async def run_check(user: dict = Depends(require_role("manager"))) -> dict:
    return notify_service.check_alerts()
