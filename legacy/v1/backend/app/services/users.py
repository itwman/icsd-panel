"""User management service — accounts, auth, 2FA.

سرویس مدیریت کاربران: حساب‌ها، احراز هویت، و 2FA.
"""
from __future__ import annotations

from app import db
from app.core import security

VALID_ROLES = {"admin", "manager", "readonly"}


class AuthError(Exception):
    pass


def create_user(username: str, password: str, role: str = "admin") -> dict:
    username = username.strip().lower()
    if not username or len(password) < 8:
        raise AuthError("نام کاربری لازم است و رمز حداقل ۸ کاراکتر / username required, password >= 8 chars")
    if role not in VALID_ROLES:
        raise AuthError(f"نقش نامعتبر / invalid role: {role}")
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise AuthError("کاربر از قبل وجود دارد / user already exists")
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            (username, security.hash_password(password), role),
        )
    db.audit(username, "user.create", f"role={role}")
    return {"username": username, "role": role}


def get_user(username: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username.strip().lower(),)).fetchone()
    return dict(row) if row else None


def count_users() -> int:
    with db.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def ensure_default_admin(default_password: str = "admin12345") -> dict | None:
    """Create an initial admin if there are no users. Returns creds if created."""
    if count_users() == 0:
        create_user("admin", default_password, "admin")
        return {"username": "admin", "password": default_password,
                "warning": "رمز پیش‌فرض را فوراً تغییر دهید / change this default password immediately"}
    return None


def authenticate(username: str, password: str, otp: str | None = None) -> dict:
    """Verify credentials (+ TOTP if enabled). Returns a JWT on success."""
    user = get_user(username)
    if not user or not security.verify_password(password, user["password_hash"]):
        db.audit(username, "auth.fail", "bad credentials")
        raise AuthError("نام کاربری یا رمز اشتباه است / invalid username or password")

    if user["totp_enabled"]:
        if not otp:
            raise AuthError("کد 2FA لازم است / OTP required")
        if not security.verify_totp(user["totp_secret"], otp):
            db.audit(username, "auth.fail", "bad otp")
            raise AuthError("کد 2FA نامعتبر / invalid OTP")

    token = security.create_access_token(user["username"], user["role"])
    db.audit(username, "auth.success", f"role={user['role']}")
    return {"access_token": token, "token_type": "bearer",
            "user": {"username": user["username"], "role": user["role"],
                     "totp_enabled": bool(user["totp_enabled"])}}


def change_password(username: str, old_password: str, new_password: str) -> dict:
    user = get_user(username)
    if not user or not security.verify_password(old_password, user["password_hash"]):
        raise AuthError("رمز فعلی اشتباه است / current password incorrect")
    if len(new_password) < 8:
        raise AuthError("رمز جدید حداقل ۸ کاراکتر / new password >= 8 chars")
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE username=?",
                     (security.hash_password(new_password), username))
    db.audit(username, "user.change_password", "")
    return {"username": username, "changed": True}


def setup_totp(username: str) -> dict:
    """Generate a TOTP secret (not yet enabled). Returns secret + provisioning URI."""
    user = get_user(username)
    if not user:
        raise AuthError("کاربر یافت نشد / user not found")
    secret = security.generate_totp_secret()
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET totp_secret=?, totp_enabled=0 WHERE username=?", (secret, username))
    return {"secret": secret, "otpauth_uri": security.totp_provisioning_uri(secret, username)}


def enable_totp(username: str, otp: str) -> dict:
    """Confirm the user can produce a valid code, then enable 2FA."""
    user = get_user(username)
    if not user or not user["totp_secret"]:
        raise AuthError("ابتدا 2FA را راه‌اندازی کنید / setup 2FA first")
    if not security.verify_totp(user["totp_secret"], otp):
        raise AuthError("کد نامعتبر / invalid code")
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET totp_enabled=1 WHERE username=?", (username,))
    db.audit(username, "user.enable_2fa", "")
    return {"username": username, "totp_enabled": True}


def disable_totp(username: str) -> dict:
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE username=?", (username,))
    db.audit(username, "user.disable_2fa", "")
    return {"username": username, "totp_enabled": False}
