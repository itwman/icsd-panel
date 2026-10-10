"""SQLite storage: one file, WAL, one connection guarded by a lock.

The hub runs as a single process (live state is in memory), so one
connection is both the simplest and the fastest choice. Every statement
here takes microseconds to a few milliseconds.

یک فایل SQLite و یک اتصال؛ hub تک‌پردازه است و نیازی به سرور دیتابیس ندارد.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable

from icsd_hub import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE users (
        id            INTEGER PRIMARY KEY,
        username      TEXT UNIQUE NOT NULL,
        pw_hash       TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'admin',      -- admin | viewer
        totp_secret   TEXT,
        totp_on       INTEGER NOT NULL DEFAULT 0,
        totp_last     INTEGER NOT NULL DEFAULT 0,         -- last used step: no replay
        created       INTEGER NOT NULL,
        last_login    INTEGER
    );
    CREATE TABLE sessions (
        id_hash  TEXT PRIMARY KEY,                         -- sha256 of the cookie
        user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created  INTEGER NOT NULL,
        seen     INTEGER NOT NULL,
        ip       TEXT,
        ua       TEXT
    );
    CREATE TABLE login_fail (ip TEXT, username TEXT, ts INTEGER);
    CREATE INDEX login_fail_ts ON login_fail(ts);

    CREATE TABLE servers (
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        created        INTEGER NOT NULL,
        enroll_hash    TEXT,
        enroll_expires INTEGER,
        secret_hash    TEXT,
        host_json      TEXT,
        agent_version  TEXT,
        last_seen      INTEGER,
        last_ip        TEXT,
        notes          TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE samples_1m (
        server_id TEXT NOT NULL, ts INTEGER NOT NULL,
        cpu REAL, cpu_max REAL, iowait REAL, steal REAL, mem REAL, swap REAL,
        load1 REAL, disk REAL, rx REAL, tx REAL, rd REAL, wr REAL, tcp REAL,
        PRIMARY KEY (server_id, ts)
    ) WITHOUT ROWID;
    CREATE TABLE samples_1h (
        server_id TEXT NOT NULL, ts INTEGER NOT NULL,
        cpu REAL, cpu_max REAL, iowait REAL, steal REAL, mem REAL, swap REAL,
        load1 REAL, disk REAL, rx REAL, tx REAL, rd REAL, wr REAL, tcp REAL,
        PRIMARY KEY (server_id, ts)
    ) WITHOUT ROWID;

    CREATE TABLE rules (
        id        INTEGER PRIMARY KEY,
        kind      TEXT UNIQUE NOT NULL,
        threshold REAL NOT NULL,
        for_s     INTEGER NOT NULL DEFAULT 0,
        severity  TEXT NOT NULL DEFAULT 'warning',          -- warning | critical
        enabled   INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE events (
        id        INTEGER PRIMARY KEY,
        rule      TEXT NOT NULL,
        server_id TEXT NOT NULL,                             -- '' for URL checks
        subject   TEXT NOT NULL,                             -- mount, container, check id…
        severity  TEXT NOT NULL,
        value     REAL,
        message   TEXT NOT NULL,
        started   INTEGER NOT NULL,
        ended     INTEGER
    );
    CREATE INDEX events_open ON events(ended);
    CREATE INDEX events_started ON events(started);

    CREATE TABLE checks (
        id        INTEGER PRIMARY KEY,
        name      TEXT NOT NULL,
        url       TEXT NOT NULL,
        interval  INTEGER NOT NULL DEFAULT 60,
        timeout   INTEGER NOT NULL DEFAULT 10,
        expect    TEXT NOT NULL DEFAULT '2xx,3xx',
        keyword   TEXT NOT NULL DEFAULT '',
        enabled   INTEGER NOT NULL DEFAULT 1,
        created   INTEGER NOT NULL
    );
    CREATE TABLE check_log (
        check_id INTEGER NOT NULL, ts INTEGER NOT NULL, ok INTEGER NOT NULL,
        ms INTEGER, code INTEGER, error TEXT,
        PRIMARY KEY (check_id, ts)
    ) WITHOUT ROWID;

    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, username TEXT,
        ip TEXT, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX audit_ts ON audit(ts);
    CREATE TABLE notify_log (
        id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, channel TEXT NOT NULL,
        ok INTEGER NOT NULL, error TEXT NOT NULL DEFAULT ''
    );
    """,
    # 2 — review fixes: re-setting 2FA keeps the old secret until confirmed;
    #     a minute row remembers how many samples it averages (merge on reconnect).
    """
    ALTER TABLE users ADD COLUMN totp_pending TEXT;
    ALTER TABLE samples_1m ADD COLUMN n INTEGER NOT NULL DEFAULT 1;
    """,
]


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            path = config.settings.db_path
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            _migrate(conn)
            _conn = conn
        return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript("BEGIN;" + sql + f"; PRAGMA user_version={i}; COMMIT;")


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in connect().execute(sql, tuple(params)).fetchall()]


def one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    with _lock:
        row = connect().execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def x(sql: str, params: Iterable[Any] = ()) -> int:
    """Execute; returns lastrowid (inserts) or rowcount."""
    with _lock:
        cur = connect().execute(sql, tuple(params))
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


def many(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    with _lock:
        c = connect()
        c.execute("BEGIN")
        try:
            c.executemany(sql, [tuple(r) for r in rows])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def now() -> int:
    return int(time.time())


# ── key/value settings ─────────────────────────────────────────────────

def get_setting(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value: Any) -> None:
    x("INSERT INTO settings(key, value) VALUES(?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def audit(username: str | None, action: str, detail: str = "", ip: str | None = None) -> None:
    x("INSERT INTO audit(ts, username, ip, action, detail) VALUES(?,?,?,?,?)",
      (now(), username, ip, action, detail[:500]))
