"""App installer endpoints — phpMyAdmin, WordPress, pgAdmin info.

نقاط پایانی نصب‌کننده‌ها.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services import apps
from app.services.apps import AppError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/apps", tags=["apps"])


class InstallRequest(BaseModel):
    domain: str
    php_version: str = "8.2"
    apply: bool = True


@router.get("/catalog")
async def catalog(user: dict = Depends(get_current_user)) -> dict:
    return {"apps": [
        {"id": "wordpress", "name": "WordPress", "desc": "CMS — نصب یک‌کلیکی با دیتابیس و wp-config"},
        {"id": "phpmyadmin", "name": "phpMyAdmin", "desc": "مدیریت گرافیکی MySQL/MariaDB"},
        {"id": "pgadmin", "name": "pgAdmin", "desc": "مدیریت PostgreSQL (راهنما)"},
    ]}


@router.post("/wordpress")
async def install_wordpress(body: InstallRequest, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return apps.install_wordpress(body.domain, body.php_version, body.apply)
    except AppError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/phpmyadmin")
async def install_phpmyadmin(body: InstallRequest, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return apps.install_phpmyadmin(body.domain, body.php_version, body.apply)
    except AppError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/pgadmin")
async def pgadmin(user: dict = Depends(get_current_user)) -> dict:
    return apps.pgadmin_info()
