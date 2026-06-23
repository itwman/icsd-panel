"""FTP account endpoints.

نقاط پایانی حساب FTP: فهرست، ساخت، تغییر رمز، فعال/غیرفعال، حذف.
رمز عبور هرگز ذخیره یا برگردانده نمی‌شود.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services import ftp as ftp_service
from app.services.ftp import FtpError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/ftp", tags=["ftp"])


class AccountIn(BaseModel):
    username: str
    password: str
    home_dir: str


class PasswordIn(BaseModel):
    password: str


class ActiveIn(BaseModel):
    active: bool


@router.get("")
async def list_accounts(user: dict = Depends(get_current_user)) -> dict:
    return {"accounts": ftp_service.list_accounts(), "server": ftp_service.server_status()}


@router.post("", status_code=201)
async def create_account(body: AccountIn, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return ftp_service.create_account(body.username, body.password, body.home_dir)
    except FtpError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{username}/password")
async def change_password(username: str, body: PasswordIn,
                          user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return ftp_service.change_password(username, body.password)
    except FtpError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{username}/active")
async def set_active(username: str, body: ActiveIn,
                     user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return ftp_service.set_active(username, body.active)
    except FtpError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{username}")
async def delete_account(username: str, remove_home: bool = False,
                         user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return ftp_service.delete_account(username, remove_home=remove_home)
    except FtpError as e:
        raise HTTPException(status_code=400, detail=str(e))
