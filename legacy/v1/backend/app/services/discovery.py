"""Vhost discovery + site health checks.

کشف خودکار vhostهای موجود روی سرور و بررسی سلامت (health check) سایت‌ها.
کانفیگ‌های Nginx را پارس می‌کند (حتی آن‌هایی که توسط پنل ساخته نشده‌اند) و وضعیت
زندهٔ هر سایت را بررسی می‌کند.
"""
from __future__ import annotations

import glob
import os
import re
import socket
import urllib.request
import ssl as ssl_mod

from app.core.osdetect import detect_os
from app import db

# Not anchored to line start — nginx directives may follow other tokens on a line.
_SERVER_NAME_RE = re.compile(r"\bserver_name\s+([^;]+);")
_ROOT_RE = re.compile(r"\broot\s+([^;]+);")
_LISTEN_RE = re.compile(r"\blisten\s+([^;]+);")


def _conf_dirs() -> list[str]:
    info = detect_os()
    dirs = ["/etc/nginx/sites-enabled", "/etc/nginx/conf.d", "/etc/nginx/sites-available"]
    # Put the active one first
    if info.family == "debian":
        return dirs
    return ["/etc/nginx/conf.d", "/etc/nginx/sites-enabled", "/etc/nginx/sites-available"]


def parse_vhost_file(path: str) -> list[dict]:
    """Parse server_name / root / listen / ssl from a single nginx conf file."""
    try:
        text = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return []
    names = _SERVER_NAME_RE.findall(text)
    roots = _ROOT_RE.findall(text)
    listens = _LISTEN_RE.findall(text)
    domains: list[str] = []
    for n in names:
        domains += [d for d in n.split() if d not in ("_", "localhost")]
    ssl_on = any("ssl" in l or "443" in l for l in listens)
    return [{
        "file": path,
        "domains": sorted(set(domains)),
        "root": roots[0].strip() if roots else None,
        "ssl": ssl_on,
        "managed_by_panel": "ICSD Panel" in text,
    }] if domains or roots else []


def discover_vhosts() -> dict:
    """Find all nginx vhosts on the server, flag which ones the panel manages."""
    with db.get_conn() as conn:
        managed_domains = {r["domain"] for r in conn.execute("SELECT domain FROM sites")}

    seen_files: set[str] = set()
    vhosts: list[dict] = []
    for d in _conf_dirs():
        for path in glob.glob(os.path.join(d, "*")):
            real = os.path.realpath(path)
            if real in seen_files or not os.path.isfile(real):
                continue
            seen_files.add(real)
            for vh in parse_vhost_file(real):
                vh["in_panel_db"] = any(dom in managed_domains for dom in vh["domains"])
                vhosts.append(vh)
    untracked = [v for v in vhosts if not v["in_panel_db"] and v["domains"]]
    return {"total": len(vhosts), "untracked_count": len(untracked), "vhosts": vhosts}


def health_check(target: str, timeout: int = 6) -> dict:
    """HTTP(S) health check for a domain or URL."""
    if not target.startswith("http"):
        url = "http://" + target
    else:
        url = target
    host = re.sub(r"^https?://", "", url).split("/")[0]
    result = {"target": target, "url": url}

    # DNS resolution
    try:
        result["resolved_ip"] = socket.gethostbyname(host)
    except OSError:
        result["resolved_ip"] = None

    # HTTP request
    ctx = ssl_mod.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl_mod.CERT_NONE
    import time as _t
    start = _t.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ICSD-Panel-HealthCheck"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            result.update(status_code=resp.status, ok=200 <= resp.status < 400,
                          response_ms=int((_t.time() - start) * 1000))
    except urllib.error.HTTPError as e:
        result.update(status_code=e.code, ok=False, response_ms=int((_t.time() - start) * 1000))
    except Exception as e:  # noqa
        result.update(status_code=None, ok=False, error=str(e)[:120])
    return result


def health_check_all() -> dict:
    """Health-check every panel-managed enabled site."""
    with db.get_conn() as conn:
        rows = conn.execute("SELECT domain FROM sites WHERE enabled=1").fetchall()
    return {"results": [health_check(r["domain"]) for r in rows]}
