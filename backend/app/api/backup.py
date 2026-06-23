"""Backup endpoints — jobs + manual run.

نقاط پایانی بک‌آپ: مدیریت زمان‌بندی و اجرای دستی.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services import backup as backup_service
from app.services.backup import BackupError
from app.services import scheduler
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/backups", tags=["backups"])


class BackupJobIn(BaseModel):
    name: str
    source_type: str               # site | database | path
    source_ref: str
    dest_type: str = "ftp"         # ftp | sftp | local
    dest_host: str | None = None
    dest_port: int | None = None
    dest_user: str | None = None
    dest_password: str | None = None
    dest_path: str = "/"
    schedule_cron: str = "0 3 * * *"
    retention: int = 7


@router.get("")
async def list_jobs(user: dict = Depends(get_current_user)) -> dict:
    jobs = backup_service.list_jobs()
    # never leak passwords to the UI
    for j in jobs:
        j.pop("dest_password", None)
    return {"jobs": jobs}


@router.post("", status_code=201)
async def create_job(body: BackupJobIn, user: dict = Depends(require_role("manager"))) -> dict:
    job = backup_service.create_job(body.model_dump())
    scheduler.reload_backup_jobs()
    job.pop("dest_password", None)
    return job


@router.post("/{job_id}/run")
async def run_now(job_id: int, dry: bool = False,
                  user: dict = Depends(require_role("manager"))) -> dict:
    job = backup_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    try:
        return backup_service.run_job(job, apply=not dry)
    except BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))


class RestoreIn(BaseModel):
    archive: str
    dry: bool = False


@router.get("/{job_id}/archives")
async def list_archives(job_id: int, user: dict = Depends(get_current_user)) -> dict:
    job = backup_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    try:
        return {"archives": backup_service.list_archives(job)}
    except BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/restore")
async def restore(job_id: int, body: RestoreIn,
                  user: dict = Depends(require_role("manager"))) -> dict:
    job = backup_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    try:
        return backup_service.restore_job(job, body.archive, apply=not body.dry)
    except BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{job_id}")
async def delete_job(job_id: int, user: dict = Depends(require_role("manager"))) -> dict:
    res = backup_service.delete_job(job_id)
    scheduler.reload_backup_jobs()
    return res
