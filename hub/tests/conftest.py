import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ICSD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ICSD_DIST_DIR", str(tmp_path / "dist"))
    monkeypatch.setenv("ICSD_PUBLIC_URL", "")
    monkeypatch.setenv("ICSD_REQUIRE_2FA", "1")
    from icsd_hub import alerts, config, db, live, notify, checks
    db.close()
    config.reload()
    live._live.clear()
    checks._state.clear()
    sent: list[str] = []
    monkeypatch.setattr(notify, "fire", sent.append)
    db.connect()
    alerts.seed()
    alerts._started = 0  # no start-up grace in tests
    yield sent
    db.close()


@pytest.fixture
def client(env):
    from icsd_hub.main import create_app
    with TestClient(create_app(), base_url="http://testserver") as c:
        c.headers["X-ICSD"] = "1"
        yield c


def make_admin(username="admin", password="correct-horse-1", role="admin"):
    from icsd_hub import auth
    return auth.create_user(username, password, role)


def login_with_2fa(client, username="admin", password="correct-horse-1", role="admin"):
    """A logged-in account that has finished 2FA setup."""
    from icsd_hub import db, security
    make_admin(username, password, role)
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    secret = client.post("/api/auth/2fa/begin").json()["secret"]
    r = client.post("/api/auth/2fa/confirm", json={"code": security.totp_now(secret)})
    assert r.status_code == 200, r.text
    return db.one("SELECT * FROM users WHERE username=?", (username,))
