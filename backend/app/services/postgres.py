"""PostgreSQL management — mirrors the MySQL service via the `psql` client.

مدیریت PostgreSQL با کلاینت psql. شناسه‌ها به‌شدت اعتبارسنجی می‌شوند تا از تزریق SQL
جلوگیری شود. عملیات با کاربر سیستمی postgres (peer auth) یا اتصال محلی انجام می‌شود.
"""
from __future__ import annotations

import re
import secrets

from app.core import oscmd
from app import db

_IDENT_RE = re.compile(r"^[A-Za-z0-9_]{1,63}$")


class PGError(Exception):
    pass


def _valid_ident(name: str, what: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise PGError(f"نام {what} نامعتبر / invalid {what} name: {name!r}")
    return name


def available() -> bool:
    return oscmd.has("psql")


def _psql(sql: str) -> oscmd.CmdResult:
    """Run SQL as the postgres superuser (peer auth via sudo -u postgres)."""
    if not available():
        raise PGError("کلاینت psql نصب نیست / psql client not installed")
    if oscmd.has("sudo"):
        return oscmd.run(["sudo", "-u", "postgres", "psql", "-tAc", sql], timeout=30)
    return oscmd.run(["psql", "-U", "postgres", "-tAc", sql], timeout=30)


def gen_password(length: int = 18) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_database(name: str, user: str | None = None, password: str | None = None,
                    apply: bool = True) -> dict:
    name = _valid_ident(name, "دیتابیس")
    user = _valid_ident(user or name, "کاربر")
    password = password or gen_password()
    # password is single-quoted in a literal; escape single quotes defensively
    safe_pw = password.replace("'", "''")
    stmts = [
        f"CREATE ROLE {user} LOGIN PASSWORD '{safe_pw}';",
        f"CREATE DATABASE {name} OWNER {user} ENCODING 'UTF8';",
        f"GRANT ALL PRIVILEGES ON DATABASE {name} TO {user};",
    ]
    result = {"engine": "postgres", "database": name, "user": user, "password": password, "applied": apply}
    if apply:
        for s in stmts:
            res = _psql(s)
            if not res.ok and "already exists" not in (res.stderr or ""):
                raise PGError(f"خطای Postgres / Postgres error: {res.stderr or res.stdout}")
    else:
        result["sql_preview"] = " ".join(stmts)

    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM databases WHERE name=?", (name,)).fetchone():
            conn.execute("UPDATE databases SET db_user=?, engine='postgres' WHERE name=?", (user, name))
        else:
            conn.execute("INSERT INTO databases (name, db_user, engine) VALUES (?,?, 'postgres')", (name, user))
    db.audit(None, "pg.create", f"{name} (user={user})")
    return result


def list_databases() -> dict:
    with db.get_conn() as conn:
        managed = [dict(r) for r in conn.execute(
            "SELECT * FROM databases WHERE engine='postgres' ORDER BY created_at DESC")]
    live = None
    if available():
        res = _psql("SELECT datname FROM pg_database WHERE datistemplate = false;")
        if res.ok:
            system = {"postgres"}
            live = [d for d in res.stdout.splitlines() if d and d not in system]
    return {"engine": "postgres", "managed": managed, "live": live}


def delete_database(name: str, drop_user: bool = True, apply: bool = True) -> dict:
    name = _valid_ident(name, "دیتابیس")
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM databases WHERE name=? AND engine='postgres'", (name,)).fetchone()
    user = row["db_user"] if row else None
    if apply:
        res = _psql(f"DROP DATABASE IF EXISTS {name};")
        if not res.ok:
            raise PGError(f"خطای Postgres / Postgres error: {res.stderr or res.stdout}")
        if drop_user and user:
            _psql(f"DROP ROLE IF EXISTS {_valid_ident(user, 'کاربر')};")
    with db.get_conn() as conn:
        conn.execute("DELETE FROM databases WHERE name=? AND engine='postgres'", (name,))
    db.audit(None, "pg.delete", name)
    return {"engine": "postgres", "database": name, "deleted": True}


def reset_password(name: str, apply: bool = True) -> dict:
    name = _valid_ident(name, "دیتابیس")
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM databases WHERE name=? AND engine='postgres'", (name,)).fetchone()
    if not row:
        raise PGError("دیتابیس یافت نشد / database not found")
    user = _valid_ident(row["db_user"], "کاربر")
    new_pw = gen_password()
    if apply:
        res = _psql(f"ALTER ROLE {user} WITH PASSWORD '{new_pw}';")
        if not res.ok:
            raise PGError(f"خطای Postgres / Postgres error: {res.stderr or res.stdout}")
    db.audit(None, "pg.reset_password", name)
    return {"engine": "postgres", "database": name, "user": user, "password": new_pw}
