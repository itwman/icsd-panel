"""Log viewer — tail nginx/site logs and panel service logs without SSH.

نمایشگر لاگ: خواندن انتهای فایل‌های لاگ nginx (access/error) و لاگ سرویس‌ها،
برای عیب‌یابی توسط کاربری که به ترمینال دسترسی ندارد. فقط-خواندنی و محدود به
مسیرهای لاگ مجاز.
"""
from __future__ import annotations

import glob
import os

from app.core import oscmd

# مسیرهای لاگی که اجازهٔ خواندن دارند (پیشوندِ مجاز).
ALLOWED_LOG_DIRS = ("/var/log/nginx", "/var/log")
MAX_LINES = 1000


class LogError(Exception):
    pass


def _safe_log_path(path: str) -> str:
    real = os.path.realpath(path)
    if not any(real == d or real.startswith(d.rstrip("/") + "/") for d in ALLOWED_LOG_DIRS):
        raise LogError("خارج از مسیرهای لاگ مجاز / outside allowed log dirs")
    if not real.endswith((".log", ".log.1")) and "/log/" not in real:
        raise LogError("فقط فایل‌های لاگ / log files only")
    return real


def list_logs() -> dict:
    """List available log files: nginx site logs + common service logs."""
    nginx: list[dict] = []
    for pattern in ("/var/log/nginx/*.log", "/var/log/nginx/*access*", "/var/log/nginx/*error*"):
        for p in glob.glob(pattern):
            if os.path.isfile(p):
                nginx.append({"name": os.path.basename(p), "path": p,
                              "size": os.path.getsize(p)})
    # de-dup
    seen = set()
    nginx_u = []
    for item in sorted(nginx, key=lambda x: x["name"]):
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        nginx_u.append(item)

    system = []
    for name, p in (("syslog", "/var/log/syslog"), ("auth.log", "/var/log/auth.log"),
                    ("mail.log", "/var/log/maillog"), ("mail.log", "/var/log/mail.log")):
        if os.path.isfile(p):
            system.append({"name": name, "path": p, "size": os.path.getsize(p)})

    services = ["icsdpanel", "nginx", "php-fpm", "mariadb", "mysql",
                "postgresql", "fail2ban", "postfix", "dovecot"]
    return {"nginx": nginx_u, "system": system, "services": services}


def tail_file(path: str, lines: int = 200) -> dict:
    """Return the last `lines` lines of an allowed log file."""
    lines = max(1, min(int(lines), MAX_LINES))
    real = _safe_log_path(path)
    if not os.path.isfile(real):
        raise LogError("فایل لاگ یافت نشد / log file not found")
    # efficient tail: read from the end in blocks
    try:
        with open(real, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        text = data.decode("utf-8", errors="replace")
        tail = "\n".join(text.splitlines()[-lines:])
    except PermissionError:
        raise LogError("دسترسی به فایل لاگ ممکن نیست / permission denied")
    return {"path": real, "lines": lines, "content": tail}


def journal(service: str, lines: int = 200) -> dict:
    """Return recent journald logs for a service via journalctl (if available)."""
    lines = max(1, min(int(lines), MAX_LINES))
    if not service.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise LogError("نام سرویس نامعتبر / invalid service name")
    if not oscmd.has("journalctl"):
        return {"service": service, "available": False,
                "hint": "journalctl در دسترس نیست / journalctl not available"}
    res = oscmd.run(["journalctl", "-u", service, "-n", str(lines),
                     "--no-pager", "--no-hostname"], timeout=20)
    if not res.ok and not res.stdout:
        return {"service": service, "available": True, "content": res.stderr or "—"}
    return {"service": service, "available": True, "content": res.stdout or "—"}
