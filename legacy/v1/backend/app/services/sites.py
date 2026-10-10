"""Site / domain management — Nginx vhost generation.

مدیریت سایت و دامنه: تولید کانفیگ Nginx از تمپلیت، فعال/غیرفعال‌سازی، تست و reload.

نکتهٔ سازگاری توزیع:
 - خانوادهٔ Debian (ubuntu/debian): sites-available + symlink در sites-enabled
 - خانوادهٔ RHEL (almalinux/rocky): قرار دادن مستقیم در conf.d
"""
from __future__ import annotations

import re
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.osdetect import detect_os
from app.core import oscmd
from app import db

# ---- Paths ----
_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "nginx"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=()),  # config files, not HTML
    keep_trailing_newline=True,
)

DEFAULT_WEBROOT_BASE = "/var/www"

# Strict domain validation — blocks anything that could break the config.
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)
_VALID_TYPES = {"static", "php", "proxy"}


class SiteError(Exception):
    """Raised on validation or system errors."""


def validate_domain(domain: str) -> str:
    domain = domain.strip().lower()
    if not _DOMAIN_RE.match(domain):
        raise SiteError(f"دامنهٔ نامعتبر / invalid domain: {domain!r}")
    return domain


def _nginx_dirs() -> tuple[Path, Path | None]:
    """Return (config_dir, enabled_dir_or_None) based on distro family."""
    info = detect_os()
    if info.family == "debian":
        return Path("/etc/nginx/sites-available"), Path("/etc/nginx/sites-enabled")
    # rhel & unknown -> conf.d (no separate enabled dir)
    return Path("/etc/nginx/conf.d"), None


def _conf_filename(domain: str) -> str:
    return f"{domain}.conf"


def render_vhost(
    *,
    domain: str,
    site_type: str = "static",
    webroot: str,
    server_names: str,
    php_version: str | None = None,
    proxy_pass: str | None = None,
    ssl_enabled: bool = False,
) -> str:
    """Render the Nginx vhost text. Pure function — easy to unit test."""
    if site_type not in _VALID_TYPES:
        raise SiteError(f"نوع سایت نامعتبر / invalid site_type: {site_type}")
    template = _env.get_template(f"{site_type}.conf.j2")
    return template.render(
        domain=domain,
        server_names=server_names,
        webroot=webroot,
        php_version=php_version or "8.2",
        proxy_pass=proxy_pass or "http://127.0.0.1:3000",
        ssl_enabled=ssl_enabled,
        ssl_cert=f"/etc/letsencrypt/live/{domain}/fullchain.pem",
        ssl_key=f"/etc/letsencrypt/live/{domain}/privkey.pem",
    )


def _write_and_enable(domain: str, content: str, enabled: bool) -> None:
    conf_dir, enabled_dir = _nginx_dirs()
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf_path = conf_dir / _conf_filename(domain)
    conf_path.write_text(content, encoding="utf-8")

    if enabled_dir is not None:  # Debian-style symlink
        enabled_dir.mkdir(parents=True, exist_ok=True)
        link = enabled_dir / _conf_filename(domain)
        if enabled:
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(conf_path)
        elif link.exists() or link.is_symlink():
            link.unlink()
    else:  # RHEL conf.d — toggle by extension
        disabled_path = conf_dir / f"{domain}.conf.disabled"
        if enabled:
            if disabled_path.exists():
                disabled_path.rename(conf_path)
        else:
            if conf_path.exists():
                conf_path.rename(disabled_path)


def create_site(
    *,
    domain: str,
    site_type: str = "static",
    aliases: str = "",
    php_version: str | None = None,
    proxy_pass: str | None = None,
    webroot: str | None = None,
    apply: bool = True,
) -> dict:
    """Create a site: persist, write vhost, enable, test, reload.

    apply=False renders+persists without touching Nginx (useful for dry-run/tests).
    """
    domain = validate_domain(domain)
    alias_list = [validate_domain(a) for a in aliases.split(",") if a.strip()]
    server_names = " ".join([domain] + alias_list)
    webroot = webroot or f"{DEFAULT_WEBROOT_BASE}/{domain}"

    # Persist metadata
    with db.get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM sites WHERE domain=?", (domain,)).fetchone()
        if exists:
            raise SiteError(f"سایت از قبل وجود دارد / site already exists: {domain}")
        conn.execute(
            """INSERT INTO sites (domain, aliases, site_type, webroot, php_version, proxy_pass, enabled)
               VALUES (?,?,?,?,?,?,1)""",
            (domain, ",".join(alias_list), site_type, webroot, php_version, proxy_pass),
        )

    content = render_vhost(
        domain=domain, site_type=site_type, webroot=webroot,
        server_names=server_names, php_version=php_version,
        proxy_pass=proxy_pass, ssl_enabled=False,
    )

    result = {"domain": domain, "applied": apply, "config_preview": content}

    if apply:
        # Create webroot with a placeholder page
        try:
            Path(webroot).mkdir(parents=True, exist_ok=True)
            index = Path(webroot) / "index.html"
            if not index.exists():
                index.write_text(
                    f"<!doctype html><meta charset=utf-8>"
                    f"<h1>{domain}</h1><p>Powered by ICSD Panel</p>",
                    encoding="utf-8",
                )
        except OSError as e:
            raise SiteError(f"ساخت webroot ناموفق / webroot create failed: {e}")

        _write_and_enable(domain, content, enabled=True)
        test = oscmd.nginx_test()
        result["nginx_test"] = {"ok": test.ok, "output": test.stderr or test.stdout}
        if test.ok:
            reload_res = oscmd.nginx_reload()
            result["nginx_reload"] = {"ok": reload_res.ok, "output": reload_res.stderr or reload_res.stdout}
        else:
            result["warning"] = "کانفیگ Nginx تست نشد؛ سایت غیرفعال شد / nginx test failed; not reloaded"

    db.audit(None, "site.create", domain)
    return result


def create_subdomain(
    *,
    parent_domain: str,
    label: str,
    site_type: str = "static",
    php_version: str | None = None,
    proxy_pass: str | None = None,
    apply: bool = True,
) -> dict:
    """Create a subdomain (e.g. label='blog', parent='example.ir' -> blog.example.ir)."""
    parent_domain = validate_domain(parent_domain)
    label = label.strip().lower()
    if not re.match(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$", label):
        raise SiteError(f"برچسب زیردامنه نامعتبر / invalid subdomain label: {label!r}")
    full = f"{label}.{parent_domain}"
    return create_site(domain=full, site_type=site_type, php_version=php_version,
                       proxy_pass=proxy_pass, apply=apply)


def list_subdomains(parent_domain: str) -> list[dict]:
    parent_domain = validate_domain(parent_domain)
    suffix = "." + parent_domain
    return [s for s in list_sites() if s["domain"].endswith(suffix)]


def list_sites() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM sites ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_site(domain: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM sites WHERE domain=?", (domain,)).fetchone()
    return dict(row) if row else None


def set_enabled(domain: str, enabled: bool, apply: bool = True) -> dict:
    site = get_site(domain)
    if not site:
        raise SiteError(f"سایت یافت نشد / site not found: {domain}")
    with db.get_conn() as conn:
        conn.execute("UPDATE sites SET enabled=? WHERE domain=?", (1 if enabled else 0, domain))
    if apply:
        _write_and_enable(domain, _current_config(site, enabled_state=enabled), enabled=enabled)
        test = oscmd.nginx_test()
        if test.ok:
            oscmd.nginx_reload()
    db.audit(None, "site.enable" if enabled else "site.disable", domain)
    return {"domain": domain, "enabled": enabled}


def _current_config(site: dict, enabled_state: bool = True) -> str:
    alias_list = [a for a in (site.get("aliases") or "").split(",") if a]
    server_names = " ".join([site["domain"]] + alias_list)
    return render_vhost(
        domain=site["domain"], site_type=site["site_type"], webroot=site["webroot"],
        server_names=server_names, php_version=site.get("php_version"),
        proxy_pass=site.get("proxy_pass"), ssl_enabled=bool(site.get("ssl_enabled")),
    )


def delete_site(domain: str, apply: bool = True, remove_webroot: bool = False) -> dict:
    site = get_site(domain)
    if not site:
        raise SiteError(f"سایت یافت نشد / site not found: {domain}")
    if apply:
        conf_dir, enabled_dir = _nginx_dirs()
        for p in [conf_dir / _conf_filename(domain),
                  conf_dir / f"{domain}.conf.disabled"]:
            if p.exists():
                p.unlink()
        if enabled_dir is not None:
            link = enabled_dir / _conf_filename(domain)
            if link.exists() or link.is_symlink():
                link.unlink()
        if remove_webroot:
            import shutil as _sh
            _sh.rmtree(site["webroot"], ignore_errors=True)
        test = oscmd.nginx_test()
        if test.ok:
            oscmd.nginx_reload()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sites WHERE domain=?", (domain,))
    db.audit(None, "site.delete", domain)
    return {"domain": domain, "deleted": True}
