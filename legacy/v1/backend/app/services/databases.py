"""MySQL / MariaDB management.

مدیریت دیتابیس MySQL/MariaDB: ساخت/حذف دیتابیس و کاربر، اعطای دسترسی.
از کلاینت `mysql` سیستم استفاده می‌کند (بدون درایور پایتونی، وابستگی کمتر).
نام‌ها به‌شدت اعتبارسنجی می‌شوند تا از تزریق SQL جلوگیری شود.
"""
from __future__ import annotations

import re
import secrets

from app.core import oscmd
from app import db

# Only safe identifier characters — blocks SQL injection via names.
_IDENT_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class DBError(Exception):
    pass


def _valid_ident(name: str, what: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise DBError(f"نام {what} نامعتبر (فقط حروف، عدد، زیرخط) / invalid {what} name: {name!r}")
    return name


def _mysql_exec(sql: str) -> oscmd.CmdResult:
    """Execute SQL via the system mysql client as root (sudo for socket auth)."""
    if not oscmd.has("mysql"):
        raise DBError("کلاینت mysql نصب نیست / mysql client not installed")
    # MariaDB root uses unix_socket auth → must run as root (via sudo when unprivileged)
    return oscmd.run_priv(["mysql", "-N", "-B", "-e", sql], timeout=30)


def gen_password(length: int = 18) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_database(name: str, user: str | None = None, password: str | None = None,
                    apply: bool = True) -> dict:
    name = _valid_ident(name, "دیتابیس")
    user = _valid_ident(user or name, "کاربر")
    password = password or gen_password()

    statements = [
        f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        f"CREATE USER IF NOT EXISTS '{user}'@'localhost' IDENTIFIED BY '{password}';",
        f"GRANT ALL PRIVILEGES ON `{name}`.* TO '{user}'@'localhost';",
        "FLUSH PRIVILEGES;",
    ]
    sql = " ".join(statements)

    result = {"database": name, "user": user, "password": password, "applied": apply}
    if apply:
        res = _mysql_exec(sql)
        if not res.ok:
            raise DBError(f"خطای MySQL / MySQL error: {res.stderr or res.stdout}")
        result["output"] = "created"
    else:
        result["sql_preview"] = sql

    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM databases WHERE name=?", (name,)).fetchone():
            conn.execute("UPDATE databases SET db_user=? WHERE name=?", (user, name))
        else:
            conn.execute("INSERT INTO databases (name, db_user, engine) VALUES (?,?, 'mysql')", (name, user))
    db.audit(None, "db.create", f"{name} (user={user})")
    return result


def list_databases() -> dict:
    """List panel-managed databases (metadata) and, if possible, live server list."""
    with db.get_conn() as conn:
        managed = [dict(r) for r in conn.execute("SELECT * FROM databases ORDER BY created_at DESC")]
    live = None
    if oscmd.has("mysql"):
        res = _mysql_exec("SHOW DATABASES;")
        if res.ok:
            system = {"information_schema", "mysql", "performance_schema", "sys"}
            live = [d for d in res.stdout.splitlines() if d and d not in system]
    return {"managed": managed, "live": live}


def delete_database(name: str, drop_user: bool = True, apply: bool = True) -> dict:
    name = _valid_ident(name, "دیتابیس")
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM databases WHERE name=?", (name,)).fetchone()
    user = row["db_user"] if row else None

    if apply:
        stmts = [f"DROP DATABASE IF EXISTS `{name}`;"]
        if drop_user and user:
            stmts.append(f"DROP USER IF EXISTS '{_valid_ident(user, 'کاربر')}'@'localhost';")
        stmts.append("FLUSH PRIVILEGES;")
        res = _mysql_exec(" ".join(stmts))
        if not res.ok:
            raise DBError(f"خطای MySQL / MySQL error: {res.stderr or res.stdout}")

    with db.get_conn() as conn:
        conn.execute("DELETE FROM databases WHERE name=?", (name,))
    db.audit(None, "db.delete", name)
    return {"database": name, "deleted": True}


def reset_password(name: str, apply: bool = True) -> dict:
    name = _valid_ident(name, "دیتابیس")
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM databases WHERE name=?", (name,)).fetchone()
    if not row:
        raise DBError("دیتابیس یافت نشد / database not found")
    user = _valid_ident(row["db_user"], "کاربر")
    new_pw = gen_password()
    if apply:
        res = _mysql_exec(
            f"ALTER USER '{user}'@'localhost' IDENTIFIED BY '{new_pw}'; FLUSH PRIVILEGES;")
        if not res.ok:
            raise DBError(f"خطای MySQL / MySQL error: {res.stderr or res.stdout}")
    db.audit(None, "db.reset_password", name)
    return {"database": name, "user": user, "password": new_pw}
