"""One-click app installers — phpMyAdmin, pgAdmin (link), WordPress.

نصب‌کننده‌های یک‌کلیکی: phpMyAdmin، و وردپرس.
آگاه از میرورهای ایرانی: می‌توان منبع دانلود را با متغیر ICSD_DOWNLOAD_MIRROR
تنظیم کرد تا محدودیت‌های شبکه دور زده شود. عملیات شبکه‌ای فقط با apply=True انجام
می‌شود؛ منطق تولید کانفیگ و wp-config مستقل و قابل تست است.
"""
from __future__ import annotations

import os
import secrets
import re

from app.core import oscmd
from app.services import sites, databases
from app import db

# Download sources — overridable via env for Iranian mirrors.
PHPMYADMIN_URL = os.environ.get(
    "ICSD_PHPMYADMIN_URL",
    "https://files.phpmyadmin.net/phpMyAdmin/5.2.1/phpMyAdmin-5.2.1-all-languages.tar.gz",
)
WORDPRESS_URL = os.environ.get(
    "ICSD_WORDPRESS_URL",
    "https://wordpress.org/latest.tar.gz",
)
# Optional generic mirror prefix (e.g. an Iranian caching proxy)
MIRROR = os.environ.get("ICSD_DOWNLOAD_MIRROR", "").rstrip("/")


class AppError(Exception):
    pass


def _mirrored(url: str) -> str:
    """If a mirror prefix is set, route the download through it."""
    if MIRROR:
        return f"{MIRROR}/{url.split('://', 1)[-1]}"
    return url


def _download_extract(url: str, dest_dir: str, strip_top: bool = True) -> oscmd.CmdResult:
    """Download a tar.gz and extract into dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)
    tarball = os.path.join("/tmp", f"icsd-dl-{secrets.token_hex(4)}.tar.gz")
    if not (oscmd.has("curl") or oscmd.has("wget")):
        raise AppError("curl/wget نصب نیست / curl or wget required")
    if oscmd.has("curl"):
        dl = oscmd.run(["curl", "-fSL", "-o", tarball, _mirrored(url)], timeout=300)
    else:
        dl = oscmd.run(["wget", "-O", tarball, _mirrored(url)], timeout=300)
    if not dl.ok:
        raise AppError(f"دانلود ناموفق / download failed: {dl.stderr or dl.stdout}")
    args = ["tar", "-xzf", tarball, "-C", dest_dir]
    if strip_top:
        args += ["--strip-components=1"]
    ext = oscmd.run(args, timeout=120)
    try:
        os.remove(tarball)
    except OSError:
        pass
    if not ext.ok:
        raise AppError(f"استخراج ناموفق / extract failed: {ext.stderr}")
    return ext


# --------------------------------------------------------------------------- #
# phpMyAdmin
# --------------------------------------------------------------------------- #
def install_phpmyadmin(domain: str, php_version: str = "8.2", apply: bool = True) -> dict:
    """Install phpMyAdmin as a managed PHP site at `domain`."""
    domain = sites.validate_domain(domain)
    webroot = f"/var/www/{domain}"
    plan = {"app": "phpmyadmin", "domain": domain, "webroot": webroot,
            "download": _mirrored(PHPMYADMIN_URL), "applied": apply}
    if not apply:
        return plan

    _download_extract(PHPMYADMIN_URL, webroot, strip_top=True)
    # blowfish secret for cookie auth
    secret = secrets.token_hex(16)
    cfg = os.path.join(webroot, "config.inc.php")
    sample = os.path.join(webroot, "config.sample.inc.php")
    if os.path.exists(sample):
        content = open(sample, encoding="utf-8", errors="ignore").read()
        content = re.sub(r"\$cfg\['blowfish_secret'\] = '.*?';",
                         f"$cfg['blowfish_secret'] = '{secret}';", content)
        open(cfg, "w", encoding="utf-8").write(content)
    sites.create_site(domain=domain, site_type="php", php_version=php_version,
                      webroot=webroot, apply=True)
    db.audit(None, "app.install", f"phpmyadmin -> {domain}")
    return {**plan, "installed": True}


# --------------------------------------------------------------------------- #
# WordPress
# --------------------------------------------------------------------------- #
def generate_wp_config(db_name: str, db_user: str, db_pass: str,
                       db_host: str = "localhost") -> str:
    """Render a wp-config.php with unique salts. Pure function — testable."""
    def salt():
        chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()"
        return "".join(secrets.choice(chars) for _ in range(64))
    keys = ["AUTH_KEY", "SECURE_AUTH_KEY", "LOGGED_IN_KEY", "NONCE_KEY",
            "AUTH_SALT", "SECURE_AUTH_SALT", "LOGGED_IN_SALT", "NONCE_SALT"]
    salts = "\n".join(f"define('{k}', '{salt()}');" for k in keys)
    return f"""<?php
define('DB_NAME', '{db_name}');
define('DB_USER', '{db_user}');
define('DB_PASSWORD', '{db_pass}');
define('DB_HOST', '{db_host}');
define('DB_CHARSET', 'utf8mb4');
define('DB_COLLATE', '');

{salts}

$table_prefix = 'wp_';
define('WP_DEBUG', false);
if ( ! defined( 'ABSPATH' ) ) {{ define( 'ABSPATH', __DIR__ . '/' ); }}
require_once ABSPATH . 'wp-settings.php';
"""


def install_wordpress(domain: str, php_version: str = "8.2", apply: bool = True) -> dict:
    """Download WordPress, create a MySQL DB, write wp-config, create the site."""
    domain = sites.validate_domain(domain)
    webroot = f"/var/www/{domain}"
    db_name = "wp_" + re.sub(r"[^a-z0-9]", "_", domain.lower())[:50]

    plan = {"app": "wordpress", "domain": domain, "webroot": webroot,
            "db_name": db_name, "download": _mirrored(WORDPRESS_URL), "applied": apply}
    if not apply:
        plan["wp_config_preview"] = generate_wp_config(db_name, db_name, "<generated>")[:400] + " ..."
        return plan

    # 1) database + user
    dbinfo = databases.create_database(db_name, apply=True)
    # 2) download WP
    _download_extract(WORDPRESS_URL, webroot, strip_top=True)
    # 3) wp-config
    open(os.path.join(webroot, "wp-config.php"), "w", encoding="utf-8").write(
        generate_wp_config(dbinfo["database"], dbinfo["user"], dbinfo["password"]))
    # 4) site
    sites.create_site(domain=domain, site_type="php", php_version=php_version,
                      webroot=webroot, apply=True)
    db.audit(None, "app.install", f"wordpress -> {domain}")
    return {**plan, "installed": True, "db_user": dbinfo["user"], "db_password": dbinfo["password"]}


def pgadmin_info() -> dict:
    """pgAdmin is best run as a Python web app; we expose guidance + a future hook."""
    return {
        "app": "pgadmin",
        "note": "pgAdmin به‌صورت وب‌اپ پایتونی نصب می‌شود؛ در فاز بعد به‌صورت سرویس مدیریت‌شده اضافه می‌شود. "
                "فعلاً برای Postgres از کلاینت psql و همین پنل استفاده کنید.",
        "recommended": "psql + ICSD Panel DB module",
    }
