"""Setup wizard status — what's installed/configured, for first-run onboarding.

وضعیت ویزارد راه‌اندازی: چه چیزی نصب/پیکربندی شده تا چک‌لیست راه‌اندازی فارسی
به کاربر تازه‌کار نشان داده شود.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core import oscmd
from app.core.deps import get_current_user
from app.services import settings_store as ss
from app import db

router = APIRouter(prefix="/api/setup", tags=["setup"])


def _count(table: str) -> int:
    try:
        with db.get_conn() as conn:
            return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    except Exception:  # noqa
        return 0


@router.get("/status")
async def status(user: dict = Depends(get_current_user)) -> dict:
    stack = {
        "nginx": oscmd.has("nginx"),
        "php": oscmd.has("php"),
        "mariadb": oscmd.has("mysql") or oscmd.has("mariadb"),
        "postgresql": oscmd.has("psql"),
        "acme": oscmd.has("acme.sh") or oscmd.has("/root/.acme.sh/acme.sh"),
        "fail2ban": oscmd.has("fail2ban-client"),
    }
    notify_on = ss.get_bool("notify.telegram_enabled") or ss.get_bool("notify.email_enabled")
    steps = {
        "stack_ready": all([stack["nginx"], stack["php"], stack["mariadb"]]),
        "password_changed": ss.get_bool("setup.password_changed", False),
        "has_site": _count("sites") > 0,
        "has_backup": _count("backup_jobs") > 0,
        "notifications": bool(notify_on),
        "wizard_done": ss.get_bool("setup.wizard_done", False),
    }
    done = sum(1 for v in steps.values() if v)
    return {"stack": stack, "steps": steps,
            "progress": round(done / len(steps) * 100)}


@router.post("/complete")
async def complete(user: dict = Depends(get_current_user)) -> dict:
    """Mark the wizard as dismissed so it won't auto-open again."""
    ss.set("setup.wizard_done", "true")
    return {"wizard_done": True}
