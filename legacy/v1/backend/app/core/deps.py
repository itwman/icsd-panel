"""FastAPI dependencies — current user & role-based access control.

وابستگی‌های FastAPI: کاربر فعلی و کنترل دسترسی نقش‌محور (RBAC).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core import security

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# Role hierarchy: higher number = more privilege
_ROLE_RANK = {"readonly": 1, "manager": 2, "admin": 3}


async def get_current_user(token: str | None = Depends(oauth2_scheme)) -> dict:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="احراز هویت لازم است / authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = security.decode_token(token)
    except security.JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"توکن نامعتبر / invalid token: {e}",
                            headers={"WWW-Authenticate": "Bearer"})
    return {"username": payload["sub"], "role": payload.get("role", "readonly")}


def require_role(min_role: str):
    """Dependency factory: require at least `min_role`."""
    needed = _ROLE_RANK[min_role]

    async def _checker(user: dict = Depends(get_current_user)) -> dict:
        if _ROLE_RANK.get(user["role"], 0) < needed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"دسترسی کافی نیست (نیازمند {min_role}) / requires {min_role}")
        return user

    return _checker
