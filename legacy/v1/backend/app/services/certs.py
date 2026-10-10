"""SSL certificate scanning — find all certs on the server and check validity/expiry.

اسکن گواهی‌های SSL سرور: یافتن همهٔ گواهی‌ها، بررسی اعتبار و تاریخ انقضا.
از دستور openssl استفاده می‌کند (روی هر سروری در دسترس است، بدون وابستگی پایتونی).
"""
from __future__ import annotations

import glob
import os
from datetime import datetime, timezone

from app.core import oscmd

# Common locations where certificates live.
CERT_GLOBS = [
    "/etc/letsencrypt/live/*/fullchain.pem",
    "/etc/letsencrypt/live/*/cert.pem",
    "/etc/ssl/certs/*.pem",
    "/etc/nginx/ssl/*.crt",
    "/etc/nginx/ssl/*.pem",
    "/root/.acme.sh/*/*.cer",
]


class CertError(Exception):
    pass


def _openssl_field(path: str, flag: str) -> str | None:
    if not oscmd.has("openssl"):
        raise CertError("openssl نصب نیست / openssl not installed")
    res = oscmd.run(["openssl", "x509", "-noout", flag, "-in", path], timeout=15)
    if not res.ok:
        return None
    out = res.stdout.strip()
    # outputs like "notAfter=Jun 23 12:00:00 2026 GMT"
    return out.split("=", 1)[1] if "=" in out else out


def _parse_openssl_date(s: str) -> datetime | None:
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def inspect_cert(path: str) -> dict | None:
    """Return metadata for a single certificate file."""
    if not os.path.exists(path):
        return None
    not_after_raw = _openssl_field(path, "-enddate")
    not_before_raw = _openssl_field(path, "-startdate")
    subject = _openssl_field(path, "-subject")
    issuer = _openssl_field(path, "-issuer")
    if not not_after_raw:
        return None
    expiry = _parse_openssl_date(not_after_raw)
    days_left = None
    status = "unknown"
    if expiry:
        days_left = (expiry - datetime.now(timezone.utc)).days
        if days_left < 0:
            status = "expired"
        elif days_left <= 14:
            status = "critical"
        elif days_left <= 30:
            status = "warning"
        else:
            status = "valid"

    # try to extract common name / SANs
    cn = None
    if subject:
        # openssl may emit "CN=foo" or "CN = foo" (newer versions), separated by / or ,
        import re as _re
        m = _re.search(r"CN\s*=\s*([^,/]+)", subject)
        if m:
            cn = m.group(1).strip()
    return {
        "path": path,
        "common_name": cn,
        "subject": subject,
        "issuer": issuer,
        "not_before": not_before_raw,
        "not_after": not_after_raw,
        "expiry_iso": expiry.isoformat() if expiry else None,
        "days_left": days_left,
        "status": status,
        "self_signed": bool(subject and issuer and subject == issuer),
    }


def scan_server(extra_globs: list[str] | None = None) -> dict:
    """Scan all known cert locations and summarize."""
    seen: set[str] = set()
    certs: list[dict] = []
    for pattern in CERT_GLOBS + (extra_globs or []):
        for path in glob.glob(pattern):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                info = inspect_cert(path)
            except CertError:
                raise
            except Exception:
                info = None
            if info:
                certs.append(info)

    certs.sort(key=lambda c: (c["days_left"] if c["days_left"] is not None else 99999))
    summary = {
        "total": len(certs),
        "expired": sum(1 for c in certs if c["status"] == "expired"),
        "critical": sum(1 for c in certs if c["status"] == "critical"),
        "warning": sum(1 for c in certs if c["status"] == "warning"),
        "valid": sum(1 for c in certs if c["status"] == "valid"),
    }
    return {"summary": summary, "certificates": certs}
