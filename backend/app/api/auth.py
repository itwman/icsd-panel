"""Authentication & account endpoints — JWT + TOTP (2FA) + RBAC.

نقاط پایانی احراز هویت و حساب کاربری: JWT + 2FA + RBAC.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services import users
from app.services.users import AuthError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str
    otp: str | None = None


class ChangePassword(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8)


class OTPConfirm(BaseModel):
    otp: str


class NewUser(BaseModel):
    username: str
    password: str = Field(..., min_length=8)
    role: str = "manager"


@router.post("/login")
async def login(body: LoginRequest) -> dict:
    try:
        return users.authenticate(body.username, body.password, body.otp)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/me")
async def me(user: dict = Depends(get_current_user)) -> dict:
    full = users.get_user(user["username"]) or {}
    return {"username": user["username"], "role": user["role"],
            "totp_enabled": bool(full.get("totp_enabled"))}


@router.post("/change-password")
async def change_password(body: ChangePassword, user: dict = Depends(get_current_user)) -> dict:
    try:
        return users.change_password(user["username"], body.old_password, body.new_password)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/2fa/setup")
async def setup_2fa(user: dict = Depends(get_current_user)) -> dict:
    try:
        return users.setup_totp(user["username"])
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/2fa/enable")
async def enable_2fa(body: OTPConfirm, user: dict = Depends(get_current_user)) -> dict:
    try:
        return users.enable_totp(user["username"], body.otp)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/2fa/disable")
async def disable_2fa(user: dict = Depends(get_current_user)) -> dict:
    return users.disable_totp(user["username"])


@router.post("/users", status_code=201)
async def create_user(body: NewUser, user: dict = Depends(require_role("admin"))) -> dict:
    try:
        return users.create_user(body.username, body.password, body.role)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
