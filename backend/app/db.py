"""Lightweight data layer over stdlib sqlite3.

لایهٔ دادهٔ سبک روی sqlite3 داخلی پایتون — بدون وابستگی سنگین (مناسب شرایط ایران).
Thread-safe via a connection-per-call helper. Good for single-server panels.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings


def _db_path() -> str:
    url = settings.database_url
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "", 1)
    return "icsdpanel.db"


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'admin',     -- admin | manager | readonly
    totp_secret TEXT,                              -- NULL until 2FA enabled
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT UNIQUE NOT NULL,
    aliases     TEXT NOT NULL DEFAULT '',          -- comma-separated extra server_names
    site_type   TEXT NOT NULL DEFAULT 'static',    -- static | php | proxy
    webroot     TEXT NOT NULL,
    php_version TEXT,                               -- e.g. 8.2 (php sites)
    proxy_pass  TEXT,                               -- upstream (proxy sites)
    ssl_enabled INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS databases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    db_user     TEXT NOT NULL,
    engine      TEXT NOT NULL DEFAULT 'mysql',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backup_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    source_type  TEXT NOT NULL,                    -- site | database | path
    source_ref   TEXT NOT NULL,                    -- domain / db name / path
    dest_type    TEXT NOT NULL DEFAULT 'ftp',      -- ftp | sftp | local
    dest_host    TEXT,
    dest_port    INTEGER,
    dest_user    TEXT,
    dest_password TEXT,
    dest_path    TEXT NOT NULL DEFAULT '/',
    schedule_cron TEXT NOT NULL DEFAULT '0 3 * * *',
    retention    INTEGER NOT NULL DEFAULT 7,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run     TEXT,
    last_status  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT,
    action     TEXT NOT NULL,
    detail     TEXT,
    ip         TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS metrics_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,           -- unix epoch
    cpu_percent REAL NOT NULL,
    mem_percent REAL NOT NULL,
    disk_percent REAL,
    net_up      REAL,
    net_down    REAL,
    load1       REAL
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics_history(ts);

CREATE TABLE IF NOT EXISTS mail_domains (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT UNIQUE NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mailboxes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    address      TEXT UNIQUE NOT NULL,      -- user@domain
    domain       TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    quota_mb     INTEGER NOT NULL DEFAULT 1024,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mail_aliases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,              -- alias@domain
    destination TEXT NOT NULL,             -- real@domain
    domain      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alert_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,              -- ssl | disk | site_down
    subject    TEXT NOT NULL,             -- domain / mount / etc.
    message    TEXT NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_alert_kind_subj ON alert_log(kind, subject);

CREATE TABLE IF NOT EXISTS ftp_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT UNIQUE NOT NULL,
    home_dir    TEXT NOT NULL,
    shell       TEXT NOT NULL DEFAULT '/usr/sbin/nologin',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cron_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cron_user   TEXT NOT NULL DEFAULT 'root',
    schedule    TEXT NOT NULL,
    command     TEXT NOT NULL,
    comment     TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pyapps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT UNIQUE NOT NULL,
    domain       TEXT,
    repo_url     TEXT NOT NULL,
    branch       TEXT NOT NULL DEFAULT 'main',
    app_type     TEXT NOT NULL DEFAULT 'django',   -- django | wsgi | asgi
    entry        TEXT NOT NULL DEFAULT '',
    port         INTEGER NOT NULL,
    app_dir      TEXT NOT NULL,
    venv_dir     TEXT NOT NULL,
    service_name TEXT NOT NULL,
    webhook_secret TEXT,
    last_deploy  TEXT,
    last_status  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Idempotent column additions for databases created by an earlier version.
# افزودن ستون‌های جدید به دیتابیس‌های قدیمی (بدون خطا اگر از قبل باشند).
_MIGRATIONS = [
    ("pyapps", "webhook_secret", "TEXT"),
]


def _migrate(conn) -> None:
    for table, column, coltype in _MIGRATIONS:
        try:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        except Exception:  # noqa — table may not exist yet; schema create handles it
            pass


def init_db() -> None:
    """Create tables if they do not exist. Called on app startup."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def audit(username: str | None, action: str, detail: str = "", ip: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (username, action, detail, ip) VALUES (?,?,?,?)",
            (username, action, detail, ip),
        )
