"""SSL certificate management via acme.sh — Iran-resilient.

مدیریت گواهی SSL با acme.sh — مقاوم در برابر محدودیت‌های ایران.
راهکار چندلایه:
  1. چالش DNS-01 به‌عنوان پیش‌فرض (نیازی به پورت ورودی باز ندارد)
  2. پشتیبانی از چند CA با fallback خودکار: letsencrypt -> zerossl -> buypass -> google
  3. پروکسی خروجی قابل‌تنظیم برای دور زدن فیلترینگ ACME (متغیر محیطی HTTPS_PROXY)
"""
from __future__ import annotations

import os
from app.core import oscmd, security  # security for nothing here but kept consistent
from app.core import osdetect
from app import db
from app.services.sites import validate_domain, get_site, _current_config, _write_and_enable, SiteError

# Preferred CA order — acme.sh server keywords.
CA_FALLBACK = ["letsencrypt", "zerossl", "buypass", "google"]

ACME_HOME = "/root/.acme.sh"
ACME_BIN = f"{ACME_HOME}/acme.sh"
CERT_BASE = "/etc/letsencrypt/live"


class SSLError(Exception):
    pass


def acme_installed() -> bool:
    return oscmd.has("acme.sh") or os.path.exists(ACME_BIN)


def _acme_cmd() -> str:
    return "acme.sh" if oscmd.has("acme.sh") else ACME_BIN


def _env_with_proxy() -> dict:
    """Inherit env; an outbound proxy can be set via ICSD_ACME_PROXY for Iran."""
    env = dict(os.environ)
    proxy = os.environ.get("ICSD_ACME_PROXY")
    if proxy:
        env["http_proxy"] = env["https_proxy"] = proxy
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = proxy
    return env


def issue_certificate(
    domain: str,
    *,
    method: str = "dns",          # dns | http
    dns_provider: str | None = None,   # e.g. dns_cf, dns_ar (arvan)
    cas: list[str] | None = None,
    webroot: str | None = None,
    apply: bool = True,
) -> dict:
    """Issue a certificate, trying each CA in order until one succeeds."""
    domain = validate_domain(domain)
    cas = cas or CA_FALLBACK

    if not acme_installed():
        raise SSLError(
            "acme.sh نصب نیست. نصب: curl https://get.acme.sh | sh "
            "(یا از میرور ایرانی) / acme.sh not installed."
        )

    base_args = [_acme_cmd(), "--issue", "-d", domain]
    if method == "dns":
        if not dns_provider:
            raise SSLError("برای روش DNS باید dns_provider مشخص شود / dns_provider required for DNS method")
        base_args += ["--dns", dns_provider]
    else:  # http-01
        wr = webroot or (get_site(domain) or {}).get("webroot")
        if not wr:
            raise SSLError("برای روش HTTP باید webroot مشخص شود / webroot required for HTTP method")
        base_args += ["-w", wr]

    attempts = []
    if apply:
        env = _env_with_proxy()
        for ca in cas:
            args = base_args + ["--server", ca]
            res = oscmd.run(args, timeout=180)
            attempts.append({"ca": ca, "ok": res.ok, "output": (res.stderr or res.stdout)[-300:]})
            if res.ok:
                _install_and_enable_ssl(domain)
                _record(domain, ca)
                db.audit(None, "ssl.issue", f"{domain} via {ca}")
                return {"domain": domain, "issued": True, "ca": ca, "attempts": attempts}
        raise SSLError(f"صدور گواهی با همهٔ CAها ناموفق بود / all CAs failed: {attempts}")

    return {"domain": domain, "issued": False, "planned_cmd": " ".join(base_args),
            "ca_order": cas, "applied": False}


def _install_and_enable_ssl(domain: str) -> None:
    """Install issued cert to the standard path and rewrite the vhost with SSL on."""
    target = f"{CERT_BASE}/{domain}"
    os.makedirs(target, exist_ok=True)
    oscmd.run([
        _acme_cmd(), "--install-cert", "-d", domain,
        "--key-file", f"{target}/privkey.pem",
        "--fullchain-file", f"{target}/fullchain.pem",
        "--reloadcmd", "systemctl reload nginx",
    ], timeout=60)
    site = get_site(domain)
    if site:
        with db.get_conn() as conn:
            conn.execute("UPDATE sites SET ssl_enabled=1 WHERE domain=?", (domain,))
        site["ssl_enabled"] = 1
        _write_and_enable(domain, _current_config(site, enabled_state=bool(site["enabled"])),
                          enabled=bool(site["enabled"]))
        if oscmd.nginx_test().ok:
            oscmd.nginx_reload()


def _record(domain: str, ca: str) -> None:
    site = get_site(domain)
    if site:
        with db.get_conn() as conn:
            conn.execute("UPDATE sites SET ssl_enabled=1 WHERE domain=?", (domain,))


def renew_all(apply: bool = True) -> dict:
    """Renew all certs (acme.sh tracks expiry; safe to run daily via scheduler)."""
    if not acme_installed():
        raise SSLError("acme.sh نصب نیست / acme.sh not installed")
    if not apply:
        return {"planned_cmd": f"{_acme_cmd()} --renew-all", "applied": False}
    res = oscmd.run([_acme_cmd(), "--renew-all"], timeout=600)
    db.audit(None, "ssl.renew_all", "ok" if res.ok else "failed")
    return {"ok": res.ok, "output": (res.stderr or res.stdout)[-500:]}


def list_certificates() -> dict:
    """List certificates known to acme.sh."""
    if not acme_installed():
        return {"installed": False, "certificates": []}
    res = oscmd.run([_acme_cmd(), "--list"], timeout=30)
    return {"installed": True, "raw": res.stdout}
