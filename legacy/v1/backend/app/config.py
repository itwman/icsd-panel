"""Application configuration / تنظیمات برنامه.

پیکربندی بدون وابستگی خارجی (فقط کتابخانهٔ استاندارد) — سبک و مقاوم.
Reads from environment variables (prefix ICSD_) and an optional .env file.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE per line). No external deps."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _get(key: str, default: str) -> str:
    return os.environ.get(f"ICSD_{key}", default)


def _get_bool(key: str, default: bool) -> bool:
    return _get(key, str(default)).lower() in ("1", "true", "yes", "on")


_load_dotenv()


@dataclass
class Settings:
    app_name: str = _get("APP_NAME", "ICSD Panel")
    host: str = _get("HOST", "0.0.0.0")
    port: int = int(_get("PORT", "8088"))
    debug: bool = _get_bool("DEBUG", False)

    # Security
    secret_key: str = _get("SECRET_KEY", secrets.token_urlsafe(48))
    access_token_expire_minutes: int = int(_get("ACCESS_TOKEN_EXPIRE_MINUTES", str(60 * 12)))

    # Database
    database_url: str = _get("DATABASE_URL", "sqlite:///./icsdpanel.db")

    # Monitoring
    metrics_interval_seconds: float = float(_get("METRICS_INTERVAL_SECONDS", "2.0"))

    # Localization
    default_locale: str = _get("DEFAULT_LOCALE", "fa")  # fa | en


settings = Settings()
