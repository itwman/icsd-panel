"""Accounts, sessions and the guards every API route goes through.

* Session cookie (HttpOnly, SameSite=Strict, Secure on https) — not a JWT in
  localStorage, which any injected script could read.
* Mandatory 2FA (configurable): until an account turns it on, the only
  things it can do are set it up or log out.
* Brute-force limit per IP and per username.
* Mutating requests need the `X-ICSD` header and a same-origin `Origin`:
  a forged form on another site cannot send either.

ورود با کوکیِ امن، رمز دومِ اجباری، محدودیتِ تلاشِ ورود، و محافظت CSRF.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response

from icsd_hub import config, db, security

COOKIE = "icsd_session"
ROLES = ("admin", "viewer")
FAIL_WINDOW = 15 * 60
MAX_FAIL_IP = 10
MAX_FAIL_USER = 8


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


@dataclass
class User:
    id: int
    username: str
    role: str
    totp_on: bool
    session: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ── accounts ───────────────────────────────────────────────────────────

def _clean_username(username: str) -> str:
    u = (username or "").strip().lower()
    if not (3 <= len(u) <= 32) or not all(ch.isalnum() or ch in "._-" for ch in u):
        raise AuthError("نام کاربری ۳ تا ۳۲ نویسه، فقط حروف لاتین، عدد و . _ -", 400)
    return u


def _check_password(password: str) -> None:
    if len(password or "") < security.MIN_PASSWORD:
        raise AuthError(f"رمز باید دست‌کم {security.MIN_PASSWORD} نویسه باشد", 400)


def create_user(username: str, password: str, role: str = "admin") -> dict:
    u = _clean_username(username)
    _check_password(password)
    if role not in ROLES:
        raise AuthError("نقش نامعتبر", 400)
    if db.one("SELECT 1 FROM users WHERE username=?", (u,)):
        raise AuthError("این نام کاربری وجود دارد", 400)
    uid = db.x("INSERT INTO users(username, pw_hash, role, created) VALUES(?,?,?,?)",
               (u, security.hash_password(password), role, db.now()))
    return {"id": uid, "username": u, "role": role}


def list_users() -> list[dict]:
    return db.q("SELECT id, username, role, totp_on, created, last_login FROM users ORDER BY id")


def delete_user(uid: int) -> None:
    target = db.one("SELECT role FROM users WHERE id=?", (uid,))
    if not target:
        raise AuthError("کاربر پیدا نشد", 404)
    if target["role"] == "admin" and db.one(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin'")["n"] <= 1:
        raise AuthError("آخرین مدیر را نمی‌شود حذف کرد", 400)
    db.x("DELETE FROM users WHERE id=?", (uid,))


def set_password(uid: int, password: str) -> None:
    _check_password(password)
    db.x("UPDATE users SET pw_hash=? WHERE id=?", (security.hash_password(password), uid))
    db.x("DELETE FROM sessions WHERE user_id=?", (uid,))  # log out everywhere


def reset_2fa(uid: int) -> None:
    db.x("UPDATE users SET totp_on=0, totp_secret=NULL, totp_pending=NULL, totp_last=0 WHERE id=?", (uid,))
    db.x("DELETE FROM sessions WHERE user_id=?", (uid,))


# ── brute force ────────────────────────────────────────────────────────

def _too_many(ip: str, username: str) -> bool:
    since = db.now() - FAIL_WINDOW
    by_ip = db.one("SELECT COUNT(*) AS n FROM login_fail WHERE ip=? AND ts>?", (ip, since))["n"]
    by_user = db.one("SELECT COUNT(*) AS n FROM login_fail WHERE username=? AND ts>?",
                     (username, since))["n"]
    return by_ip >= MAX_FAIL_IP or by_user >= MAX_FAIL_USER


def _fail(ip: str, username: str) -> None:
    db.x("INSERT INTO login_fail(ip, username, ts) VALUES(?,?,?)", (ip, username, db.now()))


def prune() -> None:
    now = db.now()
    db.x("DELETE FROM login_fail WHERE ts<?", (now - FAIL_WINDOW,))
    s = config.settings
    db.x("DELETE FROM sessions WHERE seen<? OR created<?",
         (now - s.session_idle_hours * 3600, now - s.session_max_days * 86400))


# ── login / sessions ───────────────────────────────────────────────────

def login(username: str, password: str, otp: str | None, ip: str, ua: str) -> tuple[str, User]:
    u = (username or "").strip().lower()[:64]
    if _too_many(ip, u):
        raise AuthError("تلاشِ ناموفقِ زیاد؛ ۱۵ دقیقهٔ دیگر دوباره امتحان کنید", 429)
    row = db.one("SELECT * FROM users WHERE username=?", (u,))
    ok = security.verify_password(password or "", row["pw_hash"] if row else security.DUMMY_HASH)
    if not row or not ok:
        _fail(ip, u)
        db.audit(u, "login.fail", "", ip)
        raise AuthError("نام کاربری یا رمز نادرست است")
    if row["totp_on"]:
        if not otp:
            raise AuthError("کد رمز دوم را وارد کنید", 401, {"need_otp": True})
        step = security.verify_totp(row["totp_secret"], otp, row["totp_last"])
        if not step:
            _fail(ip, u)
            db.audit(u, "login.fail", "otp", ip)
            raise AuthError("کد رمز دوم نادرست است", 401, {"need_otp": True})
        db.x("UPDATE users SET totp_last=? WHERE id=?", (step, row["id"]))
    raw = security.token()
    now = db.now()
    db.x("INSERT INTO sessions(id_hash, user_id, created, seen, ip, ua) VALUES(?,?,?,?,?,?)",
         (security.sha256(raw), row["id"], now, now, ip, (ua or "")[:200]))
    db.x("UPDATE users SET last_login=? WHERE id=?", (now, row["id"]))
    db.audit(u, "login", "", ip)
    return raw, User(row["id"], row["username"], row["role"], bool(row["totp_on"]))


def logout(raw: str) -> None:
    db.x("DELETE FROM sessions WHERE id_hash=?", (security.sha256(raw),))


def user_from_cookie(raw: str | None) -> User | None:
    if not raw or len(raw) > 200:
        return None
    s = config.settings
    h = security.sha256(raw)
    row = db.one(
        "SELECT s.created, s.seen, u.id, u.username, u.role, u.totp_on "
        "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id_hash=?", (h,))
    if not row:
        return None
    now = db.now()
    if row["seen"] < now - s.session_idle_hours * 3600 or row["created"] < now - s.session_max_days * 86400:
        db.x("DELETE FROM sessions WHERE id_hash=?", (h,))
        return None
    if now - row["seen"] > 60:  # one write a minute, not one per request
        db.x("UPDATE sessions SET seen=? WHERE id_hash=?", (now, h))
    return User(row["id"], row["username"], row["role"], bool(row["totp_on"]), h)


def set_cookie(resp: Response, raw: str) -> None:
    resp.set_cookie(COOKIE, raw, httponly=True, samesite="strict", path="/",
                    secure=bool(config.settings.secure_cookies),
                    max_age=config.settings.session_max_days * 86400)


def clear_cookie(resp: Response) -> None:
    resp.delete_cookie(COOKIE, path="/")


# ── 2FA setup ──────────────────────────────────────────────────────────

def begin_2fa(user: User, current_code: str | None = None) -> dict:
    """Start (re)configuring 2FA. The new secret stays *pending*: the old one
    keeps working until the new code is confirmed. Replacing an active 2FA
    needs a current code — a stolen session cookie must not be enough."""
    row = db.one("SELECT totp_on, totp_secret, totp_last FROM users WHERE id=?", (user.id,))
    if row and row["totp_on"]:
        step = security.verify_totp(row["totp_secret"], current_code or "", row["totp_last"])
        if not step:
            raise AuthError("برای تنظیم دوباره، کد فعلی رمز دوم را وارد کنید", 400, {"need_current": True})
        db.x("UPDATE users SET totp_last=? WHERE id=?", (step, user.id))
    secret = security.new_totp_secret()
    db.x("UPDATE users SET totp_pending=? WHERE id=?", (secret, user.id))
    return {"secret": secret, "uri": security.totp_uri(secret, user.username)}


def confirm_2fa(user: User, code: str) -> None:
    row = db.one("SELECT totp_pending FROM users WHERE id=?", (user.id,))
    if not row or not row["totp_pending"]:
        raise AuthError("اول راه‌اندازی رمز دوم را شروع کنید", 400)
    step = security.verify_totp(row["totp_pending"], code, 0)
    if not step:
        raise AuthError("کد نادرست است؛ ساعت گوشی را بررسی کنید", 400)
    db.x("UPDATE users SET totp_secret=totp_pending, totp_pending=NULL, totp_on=1, totp_last=? WHERE id=?",
         (step, user.id))
    # Other sessions were opened with the old (or no) second factor: end them.
    db.x("DELETE FROM sessions WHERE user_id=? AND id_hash<>?", (user.id, user.session))


# ── request guards ─────────────────────────────────────────────────────

def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers behind Caddy, so this is the real IP.
    return request.client.host if request.client else "?"


def _session_user(request: Request) -> User:
    user = user_from_cookie(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "ابتدا وارد شوید")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-icsd") != "1":
            raise HTTPException(403, "درخواست نامعتبر (CSRF)")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != f"{request.url.scheme}://{request.url.netloc}" and \
                origin.rstrip("/") != config.settings.public_url:
            raise HTTPException(403, "درخواست از مبدأ دیگر پذیرفته نیست")
    return user


def current_user_any(request: Request) -> User:
    """Logged in, 2FA possibly still pending (only for /me and 2FA setup)."""
    return _session_user(request)


def current_user(request: Request) -> User:
    user = _session_user(request)
    if config.settings.require_2fa and not user.totp_on:
        raise HTTPException(403, "2fa_setup_required")
    return user


def admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "این کار فقط برای مدیر است")
    return user
