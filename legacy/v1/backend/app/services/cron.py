"""Cron job management — panel-managed entries in a user's crontab.

مدیریت کرون‌جاب: ورودی‌های مدیریت‌شده توسط پنل در crontab یک کاربر سیستمی.
رویکرد امن: ورودی‌های پنل در sqlite نگه‌داری می‌شوند و crontab کاربر از روی آن‌ها
بازتولید می‌شود؛ خطوطِ غیرمدیریت‌شدهٔ کاربر دست‌نخورده باقی می‌مانند (با مرز
نشانه‌گذاری‌شده).
"""
from __future__ import annotations

import re
import subprocess

from app.core import oscmd
from app import db

BEGIN = "# >>> ICSD Panel managed (do not edit) >>>"
END = "# <<< ICSD Panel managed <<<"
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Basic 5-field cron validation (allows numbers, *, ranges, lists, steps, names).
_FIELD = r"(\*|[0-9]{1,2}|[a-zA-Z]{3})([-,/][0-9a-zA-Z]{1,2})*"
_CRON_RE = re.compile(r"^\s*" + r"\s+".join([_FIELD] * 5) + r"\s*$")


class CronError(Exception):
    pass


def validate_user(u: str) -> str:
    u = (u or "").strip().lower()
    if not _USER_RE.match(u):
        raise CronError("نام کاربر نامعتبر / invalid user")
    if not oscmd.run(["id", u]).ok:
        raise CronError(f"کاربر سیستمی وجود ندارد / system user not found: {u}")
    return u


def validate_schedule(expr: str) -> str:
    expr = expr.strip()
    aliases = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0",
               "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@reboot": "@reboot"}
    if expr in aliases:
        return expr
    if not _CRON_RE.match(expr):
        raise CronError("عبارت cron نامعتبر (۵ فیلد: دقیقه ساعت روز ماه روزهفته) / invalid cron expression")
    return expr


def _read_raw(user: str) -> str:
    res = oscmd.run_priv(["crontab", "-l", "-u", user])
    if not res.ok:
        # "no crontab for user" is normal -> empty
        return ""
    return res.stdout or ""


def _strip_managed(raw: str) -> str:
    """Remove the panel-managed block, return the user's own lines."""
    if BEGIN not in raw:
        return raw.rstrip("\n")
    out, skipping = [], False
    for line in raw.splitlines():
        if line.strip() == BEGIN:
            skipping = True
            continue
        if line.strip() == END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip("\n")


def _render_managed() -> str:
    rows = list_jobs()
    lines = [BEGIN]
    for j in rows:
        if not j["enabled"]:
            lines.append(f"# (disabled) {j['schedule']} {j['command']}  # icsd-id:{j['id']}")
            continue
        comment = f"  # icsd-id:{j['id']}" + (f" {j['comment']}" if j["comment"] else "")
        lines.append(f"{j['schedule']} {j['command']}{comment}")
    lines.append(END)
    return "\n".join(lines)


def _apply_crontab(user: str) -> None:
    """Rewrite the user's crontab = their own lines + panel-managed block."""
    own = _strip_managed(_read_raw(user))
    managed = _render_managed()
    content = (own + "\n\n" if own else "") + managed + "\n"
    proc = subprocess.run(oscmd.sudo_prefix() + ["crontab", "-u", user, "-"], input=content,
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise CronError(f"نوشتن crontab ناموفق / crontab write failed: {proc.stderr[:200]}")


# --------------------------------------------------------------------------- #
# CRUD (db-backed; crontab regenerated on every change)
# --------------------------------------------------------------------------- #
def create_job(user: str, schedule: str, command: str, comment: str = "",
               apply: bool = True) -> dict:
    user = validate_user(user)
    schedule = validate_schedule(schedule)
    command = command.strip()
    if not command:
        raise CronError("دستور خالی است / empty command")
    if "\n" in command or "\r" in command:
        raise CronError("دستور نباید چندخطی باشد / command must be single line")
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO cron_jobs (cron_user, schedule, command, comment) VALUES (?,?,?,?)",
            (user, schedule, command, comment),
        )
        job_id = cur.lastrowid
    if apply:
        _apply_crontab(user)
    db.audit(None, "cron.create", f"{user}: {schedule} {command}")
    return {"id": job_id, "cron_user": user, "schedule": schedule,
            "command": command, "comment": comment, "enabled": 1}


def set_enabled(job_id: int, enabled: bool) -> dict:
    job = get_job(job_id)
    if not job:
        raise CronError("کرون‌جاب یافت نشد / job not found")
    with db.get_conn() as conn:
        conn.execute("UPDATE cron_jobs SET enabled=? WHERE id=?",
                     (1 if enabled else 0, job_id))
    _apply_crontab(job["cron_user"])
    db.audit(None, "cron.enabled", f"{job_id}={enabled}")
    return {"id": job_id, "enabled": enabled}


def delete_job(job_id: int) -> dict:
    job = get_job(job_id)
    if not job:
        raise CronError("کرون‌جاب یافت نشد / job not found")
    with db.get_conn() as conn:
        conn.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
    _apply_crontab(job["cron_user"])
    db.audit(None, "cron.delete", str(job_id))
    return {"id": job_id, "deleted": True}


def get_job(job_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM cron_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs() -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM cron_jobs ORDER BY id")]


def system_users() -> list[str]:
    """Common targets for cron jobs (best-effort)."""
    users = ["root", "www-data", "nginx"]
    found = []
    for u in users:
        if oscmd.run(["id", u]).ok:
            found.append(u)
    return found or ["root"]
