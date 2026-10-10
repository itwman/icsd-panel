"""Troubleshooter endpoints — run server health checks.

نقاط پایانی عیب‌یاب: اجرای بررسی سلامت سرور و دریافت یافته‌ها با راهکار.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.services import troubleshoot
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/troubleshoot", tags=["troubleshoot"])


@router.post("/run")
async def run_checks(user: dict = Depends(get_current_user)) -> dict:
    """Run all health checks (read-only; safe for any authenticated role)."""
    return troubleshoot.run_all()
