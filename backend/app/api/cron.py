"""Cron job endpoints.

نقاط پایانی کرون‌جاب: فهرست، افزودن، فعال/غیرفعال، حذف.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services import cron as cron_service
from app.services.cron import CronError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/cron", tags=["cron"])


class CronIn(BaseModel):
    cron_user: str = "root"
    schedule: str
    command: str
    comment: str = ""


class EnabledIn(BaseModel):
    enabled: bool


@router.get("")
async def list_jobs(user: dict = Depends(get_current_user)) -> dict:
    return {"jobs": cron_service.list_jobs(), "users": cron_service.system_users()}


@router.post("", status_code=201)
async def create_job(body: CronIn, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return cron_service.create_job(body.cron_user, body.schedule, body.command, body.comment)
    except CronError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/enabled")
async def set_enabled(job_id: int, body: EnabledIn,
                      user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return cron_service.set_enabled(job_id, body.enabled)
    except CronError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{job_id}")
async def delete_job(job_id: int, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return cron_service.delete_job(job_id)
    except CronError as e:
        raise HTTPException(status_code=400, detail=str(e))
