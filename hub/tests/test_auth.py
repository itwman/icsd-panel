"""Login, 2FA, sessions, CSRF, roles — the things v1 got wrong."""
from conftest import login_with_2fa, make_admin

from icsd_hub import auth, db, security


def test_no_default_password_exists(client):
    """v1 created admin/admin12345 on first start. v2 creates nobody."""
    assert db.q("SELECT * FROM users") == []
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin12345"})
    assert r.status_code == 401


def test_every_api_route_needs_a_session(client):
    """v1 had 16 routes (monitoring, system info…) open to anyone."""
    for path in ("/api/overview", "/api/me", "/api/alerts", "/api/checks", "/api/users",
                 "/api/settings/notify", "/api/audit", "/api/servers/sxxxx",
                 "/api/servers/sxxxx/series"):
        assert client.get(path).status_code == 401, path


def test_login_needs_the_csrf_header(client):
    make_admin()
    r = client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-1"},
                    headers={"X-ICSD": ""})
    assert r.status_code == 403


def test_session_cookie_is_httponly_and_strict(client):
    make_admin()
    r = client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-1"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    raw = client.cookies.get(auth.COOKIE)
    # Only the hash is stored: a leaked database holds no usable session.
    assert db.one("SELECT 1 FROM sessions WHERE id_hash=?", (raw,)) is None
    assert db.one("SELECT 1 FROM sessions WHERE id_hash=?", (security.sha256(raw),))


def test_2fa_is_mandatory_before_anything_else(client):
    make_admin()
    client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-1"})
    assert client.get("/api/me").json()["must_setup_2fa"] is True
    r = client.get("/api/overview")
    assert r.status_code == 403 and r.json()["detail"] == "2fa_setup_required"
    assert client.post("/api/servers", json={"name": "x"}).status_code == 403


def test_2fa_setup_then_login_needs_code(client):
    make_admin()
    client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-1"})
    begin = client.post("/api/auth/2fa/begin").json()
    assert begin["qr_svg"].startswith("<svg") and begin["uri"].startswith("otpauth://totp/")
    assert client.post("/api/auth/2fa/confirm", json={"code": "000000"}).status_code == 400
    code = security.totp_now(begin["secret"])
    assert client.post("/api/auth/2fa/confirm", json={"code": code}).status_code == 200
    assert client.get("/api/overview").status_code == 200

    client.post("/api/auth/logout")
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-1"})
    assert r.status_code == 401 and r.json()["detail"]["need_otp"] is True
    # The code already used for setup cannot be replayed.
    r = client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-1", "otp": code})
    assert r.status_code == 401


def test_totp_replay_is_rejected():
    secret = security.new_totp_secret()
    at = 1_800_000_000
    code = security.totp_now(secret, at)
    step = security.verify_totp(secret, code, 0, at)
    assert step
    assert security.verify_totp(secret, code, step, at) == 0
    assert security.verify_totp(secret, "12345", 0, at) == 0


def test_brute_force_is_limited(client):
    make_admin()
    for _ in range(auth.MAX_FAIL_USER):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "nope-nope-nope"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-1"})
    assert r.status_code == 429


def test_mutations_need_header_and_same_origin(client):
    login_with_2fa(client)
    assert client.post("/api/servers", json={"name": "a"}, headers={"X-ICSD": ""}).status_code == 403
    r = client.post("/api/servers", json={"name": "a"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.post("/api/servers", json={"name": "a"}, headers={"Origin": "http://testserver"}).status_code == 200


def test_viewer_is_read_only(client):
    login_with_2fa(client, "eve", "viewer-pass-1", role="viewer")
    assert client.get("/api/overview").status_code == 200
    assert client.post("/api/servers", json={"name": "x"}).status_code == 403
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/settings/notify").status_code == 403


def test_password_change_ends_sessions(client):
    login_with_2fa(client)
    r = client.post("/api/me/password", json={"old": "correct-horse-1", "new": "short"})
    assert r.status_code == 400
    r = client.post("/api/me/password", json={"old": "correct-horse-1", "new": "a-much-better-pass"})
    assert r.status_code == 200
    assert db.one("SELECT COUNT(*) AS n FROM sessions")["n"] == 0


def test_last_admin_cannot_be_deleted(env):
    a = make_admin()
    try:
        auth.delete_user(a["id"])
        raise AssertionError("must refuse")
    except auth.AuthError:
        pass


def test_security_headers(client):
    r = client.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/docs").status_code == 404  # no public API explorer
