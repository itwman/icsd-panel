"""Log viewer endpoints — list logs, tail a file, read a service journal.

نقاط پایانی نمایشگر لاگ: فهرست لاگ‌ها، خواندن انتهای فایل، و لاگ سرویس‌ها.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.services import logs as logs_service
from app.services.logs import LogError
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def list_logs(user: dict = Depends(get_current_user)) -> dict:
    return logs_service.list_logs()


@router.get("/tail")
async def tail(path: str, lines: int = 200, user: dict = Depends(get_current_user)) -> dict:
    try:
        return logs_service.tail_file(path, lines)
    except LogError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/journal")
async def journal(service: str, lines: int = 200, user: dict = Depends(get_current_user)) -> dict:
    try:
        return logs_service.journal(service, lines)
    except LogError as e:
        raise HTTPException(status_code=400, detail=str(e))
