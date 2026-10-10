"""Key/value settings store backed by the settings table.

ذخیرهٔ تنظیمات کلید/مقدار (مثل پیکربندی اعلان‌ها) در دیتابیس.
مقادیر به‌صورت رشته ذخیره می‌شوند؛ کمک‌متدها برای bool/json هم هست.
"""
from __future__ import annotations

import json

from app import db


def get(key: str, default: str | None = None) -> str | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set(key: str, value: str | None) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (key, value),
        )


def get_bool(key: str, default: bool = False) -> bool:
    v = get(key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def get_int(key: str, default: int) -> int:
    v = get(key)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def get_many(prefix: str) -> dict:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",))
        return {r["key"]: r["value"] for r in rows}


def set_many(items: dict) -> None:
    with db.get_conn() as conn:
        for k, v in items.items():
            sv = json.dumps(v) if isinstance(v, (dict, list)) else (None if v is None else str(v))
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?,?,datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                (k, sv),
            )
