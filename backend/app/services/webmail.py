"""Webmail installer — Roundcube as a managed PHP site.

نصب‌کنندهٔ وب‌میل: Roundcube به‌صورت یک سایت PHP مدیریت‌شده. الگوی نصب مشابه
ماژول apps است (دانلود، دیتابیس، تولید config، ساخت سایت) و از میرور ایرانی
(ICSD_DOWNLOAD_MIRROR) پشتیبانی می‌کند. به سرور IMAP/SMTP محلی وصل می‌شود.
"""
from __future__ import annotations

import os
import re
import secrets

from app.core import oscmd
from app.services import sites, databases, apps
from app import db

ROUNDCUBE_URL = os.environ.get(
    "ICSD_ROUNDCUBE_URL",
    "https://github.com/roundcube/roundcubemail/releases/download/1.6.7/"
    "roundcubemail-1.6.7-complete.tar.gz",
)


class WebmailError(Exception):
    pass


def generate_config(db_name: str, db_user: str, db_pass: str,
                    imap_host: str = "localhost", smtp_host: str = "localhost",
                    db_host: str = "localhost") -> str:
    """Render Roundcube config/config.inc.php. Pure function — testable."""
    des_key = secrets.token_hex(12)  # 24 chars required by Roundcube
    dsn = f"mysql://{db_user}:{db_pass}@{db_host}/{db_name}"
    return f"""<?php
$config = [];
$config['db_dsnw'] = '{dsn}';
$config['imap_host'] = '{imap_host}:143';
$config['smtp_host'] = '{smtp_host}:587';
$config['smtp_user'] = '%u';
$config['smtp_pass'] = '%p';
$config['support_url'] = '';
$config['product_name'] = 'ICSD Webmail';
$config['des_key'] = '{des_key}';
$config['plugins'] = ['archive', 'zipdownload', 'managesieve'];
$config['language'] = 'fa_IR';
$config['enable_installer'] = false;
"""


def install(domain: str, php_version: str = "8.2",
            imap_host: str = "localhost", smtp_host: str = "localhost",
            apply: bool = True) -> dict:
    """Download Roundcube, create a DB, write config, and create the site."""
    domain = sites.validate_domain(domain)
    webroot = f"/var/www/{domain}"
    db_name = "rc_" + re.sub(r"[^a-z0-9]", "_", domain.lower())[:48]

    plan = {"app": "roundcube", "domain": domain, "webroot": webroot,
            "db_name": db_name, "download": apps._mirrored(ROUNDCUBE_URL), "applied": apply}
    if not apply:
        plan["config_preview"] = generate_config(db_name, db_name, "<generated>",
                                                  imap_host, smtp_host)[:400] + " ..."
        return plan

    # 1) database + user
    dbinfo = databases.create_database(db_name, apply=True)
    # 2) download Roundcube
    apps._download_extract(ROUNDCUBE_URL, webroot, strip_top=True)
    # 3) initialise schema (best effort — needs mysql client)
    schema = os.path.join(webroot, "SQL", "mysql.initial.sql")
    if oscmd.has("mysql") and os.path.isfile(schema):
        import subprocess
        sql = open(schema, encoding="utf-8", errors="ignore").read()
        subprocess.run(["mysql", dbinfo["database"]], input=sql,
                       capture_output=True, text=True, timeout=120)
    # 4) config
    cfg_dir = os.path.join(webroot, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    open(os.path.join(cfg_dir, "config.inc.php"), "w", encoding="utf-8").write(
        generate_config(dbinfo["database"], dbinfo["user"], dbinfo["password"],
                        imap_host, smtp_host))
    # 5) site
    sites.create_site(domain=domain, site_type="php", php_version=php_version,
                      webroot=webroot, apply=True)
    db.audit(None, "webmail.install", f"roundcube -> {domain}")
    return {**plan, "installed": True, "db_user": dbinfo["user"],
            "db_password": dbinfo["password"]}
