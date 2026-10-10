"""Settings from the environment. Nothing secret lives in code.

همهٔ تنظیمات از متغیرهای محیطی (فایل .env کنار docker-compose) خوانده می‌شوند.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("ICSD_DATA_DIR", "./data")))
    # The address people and agents use, e.g. https://panel.example.com
    public_url: str = field(default_factory=lambda: os.environ.get("ICSD_PUBLIC_URL", "").rstrip("/"))
    # Agent binaries + SHA256SUMS, built into the Docker image.
    dist_dir: Path = field(default_factory=lambda: Path(os.environ.get("ICSD_DIST_DIR", "./dist")))
    # Every account must turn on 2FA before it can do anything.
    require_2fa: bool = field(default_factory=lambda: _bool("ICSD_REQUIRE_2FA", True))
    # Cookies get `Secure` when the panel is served over https.
    secure_cookies: bool | None = None
    sample_interval: int = field(default_factory=lambda: int(os.environ.get("ICSD_SAMPLE_INTERVAL", "5")))
    session_idle_hours: int = 12
    session_max_days: int = 7
    keep_minutes_days: int = 15   # per-minute history
    keep_hours_days: int = 400    # per-hour history
    keep_events_days: int = 180

    def __post_init__(self) -> None:
        if self.secure_cookies is None:
            env = os.environ.get("ICSD_SECURE_COOKIES")
            self.secure_cookies = _bool("ICSD_SECURE_COOKIES", True) if env else \
                self.public_url.startswith("https://")
        self.sample_interval = max(2, min(self.sample_interval, 60))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.db"


settings = Settings()


def reload() -> Settings:
    """Tests change the environment and call this."""
    global settings
    settings = Settings()
    return settings
