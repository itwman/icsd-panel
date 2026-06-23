"""Notifications & alerts — Telegram + email (SMTP), with alert checks.

اعلان و هشدار: ارسال از طریق تلگرام و ایمیل (SMTP)، و بررسی شرایط هشدار
(انقضای SSL، پرشدن دیسک، داون‌شدن سایت). تلگرام با urllib استاندارد و ایمیل با
smtplib استاندارد ارسال می‌شود — بدون وابستگی اضافه.

تنظیمات از جدول settings با پیشوند notify. خوانده می‌شوند:
  notify.telegram_enabled / notify.telegram_token / notify.telegram_chat_id
  notify.email_enabled / notify.smtp_host / notify.smtp_port / notify.smtp_user
  notify.smtp_password / notify.smtp_from / notify.email_to / notify.smtp_tls
  notify.ssl_days (آستانهٔ هشدار انقضای گواهی) / notify.disk_percent (آستانهٔ دیسک)
"""
from __future__ import annotations

import json
import logging
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage

from app import db
from app.services import settings_store as ss

log = logging.getLogger("icsd.notify")


class NotifyError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #
def send_telegram(text: str) -> dict:
    token = ss.get("notify.telegram_token")
    chat_id = ss.get("notify.telegram_chat_id")
    if not token or not chat_id:
        raise NotifyError("توکن یا chat_id تلگرام تنظیم نشده / telegram not configured")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            raise NotifyError(f"تلگرام خطا داد / telegram error: {body.get('description')}")
        return {"channel": "telegram", "sent": True}
    except NotifyError:
        raise
    except Exception as e:  # noqa
        raise NotifyError(f"ارسال تلگرام ناموفق / telegram send failed: {e}")


def send_email(subject: str, body: str) -> dict:
    host = ss.get("notify.smtp_host")
    port = ss.get_int("notify.smtp_port", 587)
    user = ss.get("notify.smtp_user")
    password = ss.get("notify.smtp_password")
    sender = ss.get("notify.smtp_from") or user
    to = ss.get("notify.email_to")
    use_tls = ss.get_bool("notify.smtp_tls", True)
    if not host or not to or not sender:
        raise NotifyError("پیکربندی SMTP ناقص است / SMTP not configured")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as server:
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                if use_tls:
                    server.starttls()
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        return {"channel": "email", "sent": True}
    except Exception as e:  # noqa
        raise NotifyError(f"ارسال ایمیل ناموفق / email send failed: {e}")


def notify(subject: str, body: str) -> dict:
    """Dispatch a message to all enabled channels. Errors are collected, not raised."""
    results: list[dict] = []
    if ss.get_bool("notify.telegram_enabled", False):
        try:
            results.append(send_telegram(f"<b>{subject}</b>\n{body}"))
        except NotifyError as e:
            results.append({"channel": "telegram", "sent": False, "error": str(e)})
    if ss.get_bool("notify.email_enabled", False):
        try:
            results.append(send_email(subject, body))
        except NotifyError as e:
            results.append({"channel": "email", "sent": False, "error": str(e)})
    return {"results": results}


def test() -> dict:
    """Send a test message through all enabled channels."""
    return notify("ICSD Panel — پیام آزمایشی",
                  "این یک پیام آزمایشی از پنل ICSD است. اگر آن را دریافت کردید، "
                  "اعلان‌ها درست کار می‌کنند. ✓")


# --------------------------------------------------------------------------- #
# Alert de-duplication
# --------------------------------------------------------------------------- #
def _already_alerted(kind: str, subject: str, within_hours: int = 20) -> bool:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM alert_log WHERE kind=? AND subject=? AND sent=1 "
            "AND created_at > datetime('now', ?) LIMIT 1",
            (kind, subject, f"-{within_hours} hours"),
        ).fetchone()
    return row is not None


def _record_alert(kind: str, subject: str, message: str, sent: bool) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO alert_log (kind, subject, message, sent) VALUES (?,?,?,?)",
            (kind, subject, message, 1 if sent else 0),
        )


def _emit(kind: str, subject: str, message: str) -> dict:
    """Send an alert if not recently sent for the same kind+subject."""
    if _already_alerted(kind, subject):
        return {"kind": kind, "subject": subject, "skipped": "already alerted"}
    res = notify(f"⚠️ هشدار ICSD: {subject}", message)
    sent = any(r.get("sent") for r in res.get("results", []))
    _record_alert(kind, subject, message, sent)
    return {"kind": kind, "subject": subject, "sent": sent, "channels": res["results"]}


# --------------------------------------------------------------------------- #
# Alert checks
# --------------------------------------------------------------------------- #
def check_alerts() -> dict:
    """Run all alert checks; send notifications for any breaches. Returns a report."""
    fired: list[dict] = []

    # 1) SSL certificates nearing expiry
    ssl_days = ss.get_int("notify.ssl_days", 14)
    try:
        from app.services import certs
        report = certs.scan_server()
        for c in report.get("certificates", []):
            days = c.get("days_left")
            if days is None:
                continue
            if days <= ssl_days:
                cn = c.get("common_name") or c.get("path", "?")
                msg = (f"گواهی <code>{cn}</code> تا {days} روز دیگر منقضی می‌شود. "
                       f"برای تمدید به بخش SSL مراجعه کنید.")
                fired.append(_emit("ssl", str(cn), msg))
    except Exception as e:  # noqa
        log.error("ssl alert check failed: %s", e)

    # 2) Disk usage over threshold
    disk_pct = ss.get_int("notify.disk_percent", 90)
    try:
        from app.services import metrics
        for d in metrics.disk_metrics():
            if d.get("percent", 0) >= disk_pct:
                mp = d.get("mountpoint", "/")
                msg = (f"مصرف دیسک <code>{mp}</code> به {d['percent']}% رسیده "
                       f"(آستانه {disk_pct}%). فضای خالی را افزایش دهید.")
                fired.append(_emit("disk", mp, msg))
    except Exception as e:  # noqa
        log.error("disk alert check failed: %s", e)

    # 3) Managed sites that are down
    try:
        from app.services import discovery
        res = discovery.health_check_all()
        for r in res.get("results", []):
            if not r.get("ok"):
                tgt = r.get("target", "?")
                detail = r.get("error") or f"HTTP {r.get('status_code')}"
                msg = f"سایت <code>{tgt}</code> پاسخ نمی‌دهد ({detail})."
                fired.append(_emit("site_down", str(tgt), msg))
    except Exception as e:  # noqa
        log.error("site alert check failed: %s", e)

    sent = sum(1 for f in fired if f.get("sent"))
    return {"checked": True, "alerts": fired, "sent": sent}
