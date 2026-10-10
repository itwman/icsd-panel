"""Security primitives — password hashing, JWT (HS256), TOTP (RFC 6238).

ابزارهای امنیتی: هش رمز عبور با bcrypt، توکن JWT و TOTP با کتابخانهٔ استاندارد.
JWT و TOTP با stdlib پیاده شده‌اند تا وابستگی کمتر شود (مناسب شرایط ایران).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
from datetime import datetime, timedelta, timezone

import bcrypt

from app.config import settings

# --------------------------------------------------------------------------- #
# Password hashing (bcrypt)
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# JWT (HS256) — minimal, stdlib only
# --------------------------------------------------------------------------- #
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


class JWTError(Exception):
    pass


def create_access_token(subject: str, role: str = "admin", minutes: int | None = None) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=minutes or settings.access_token_expire_minutes)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": subject, "role": role, "exp": int(exp.timestamp())}
    seg = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + \
          _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(settings.secret_key.encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64url(sig)


def decode_token(token: str) -> dict:
    try:
        h_seg, p_seg, s_seg = token.split(".")
    except ValueError:
        raise JWTError("malformed token")
    expected = hmac.new(settings.secret_key.encode(), f"{h_seg}.{p_seg}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url(expected), s_seg):
        raise JWTError("bad signature")
    payload = json.loads(_b64url_decode(p_seg))
    if payload.get("exp", 0) < int(time.time()):
        raise JWTError("token expired")
    return payload


# --------------------------------------------------------------------------- #
# TOTP (RFC 6238) — Google Authenticator compatible, stdlib only
# --------------------------------------------------------------------------- #
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def generate_totp_secret(length: int = 20) -> str:
    """Random base32 secret."""
    raw = os.urandom(length)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    pad = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret_b32: str, code: str, window: int = 1, period: int = 30) -> bool:
    """Verify a TOTP code, allowing +/- `window` time steps for clock drift."""
    if not code or not code.isdigit():
        return False
    counter = int(time.time() // period)
    for w in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret_b32, counter + w), code.zfill(6)):
            return True
    return False


def totp_provisioning_uri(secret_b32: str, account: str, issuer: str = "ICSD Panel") -> str:
    """otpauth:// URI for QR codes in authenticator apps."""
    from urllib.parse import quote
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")
