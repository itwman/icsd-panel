"""FTP account management — system users chrooted to a web directory.

مدیریت حساب‌های FTP: کاربران سیستمیِ کم‌دسترسی که به یک پوشهٔ وب محدود (chroot)
می‌شوند. مناسب vsftpd/pure-ftpd که از کاربران سیستمی استفاده می‌کنند. متادیتا در
sqlite ذخیره و رمز عبور هرگز نگه‌داری نمی‌شود (فقط روی سیستم set می‌شود).

نکتهٔ امنیتی: عملیات نیازمند root (useradd/chpasswd/userdel) از طریق oscmd اجرا
می‌شوند؛ در معماری نهایی این فراخوانی‌ها از عامل privileged عبور می‌کنند.
"""
from __future__ import annotations

import re
import subprocess

from app.core import oscmd
from app import db

FTP_GROUP = "icsdftp"
DEFAULT_SHELL = "/usr/sbin/nologin"
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{2,31}$")


class FtpError(Exception):
    pass


def validate_username(username: str) -> str:
    u = username.strip().lower()
    if not _USER_RE.match(u):
        raise FtpError("نام کاربری نامعتبر (a-z,0-9,_,- و ۳ تا ۳۲ نویسه) / invalid username")
    # block names that collide with important system accounts
    if u in ("root", "admin", "daemon", "bin", "sys", "www-data", "nginx", "mysql", "postgres"):
        raise FtpError("این نام کاربری مجاز نیست / reserved username")
    return u


def _ensure_group() -> None:
    if not oscmd.run(["getent", "group", FTP_GROUP]).ok:
        oscmd.run_priv(["groupadd", FTP_GROUP])


def _set_password(username: str, password: str) -> None:
    if not password or len(password) < 8:
        raise FtpError("رمز عبور حداقل ۸ نویسه / password must be ≥ 8 chars")
    # chpasswd reads "user:password" from stdin (with sudo when unprivileged)
    proc = subprocess.run(oscmd.sudo_prefix() + ["chpasswd"], input=f"{username}:{password}",
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise FtpError(f"تنظیم رمز ناموفق / set password failed: {proc.stderr[:200]}")


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def create_account(username: str, password: str, home_dir: str,
                   apply: bool = True) -> dict:
    username = validate_username(username)
    if not home_dir.startswith(("/var/www", "/home", "/srv")):
        raise FtpError("مسیر خانه باید زیر /var/www یا /home یا /srv باشد / home must be under allowed roots")
    existing = get_account(username)
    if existing:
        raise FtpError("حساب از قبل وجود دارد / account already exists")

    plan = {"username": username, "home_dir": home_dir, "shell": DEFAULT_SHELL,
            "group": FTP_GROUP, "applied": apply}
    if not apply:
        return plan

    _ensure_group()
    # create the user: own home, chroot-friendly, no login shell
    res = oscmd.run_priv(["useradd", "-m", "-d", home_dir, "-s", DEFAULT_SHELL,
                          "-g", FTP_GROUP, username])
    if not res.ok and "already exists" not in (res.stderr or ""):
        raise FtpError(f"ساخت کاربر ناموفق / useradd failed: {res.stderr or res.stdout}")
    _set_password(username, password)

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO ftp_accounts (username, home_dir, shell) VALUES (?,?,?)",
            (username, home_dir, DEFAULT_SHELL),
        )
    db.audit(None, "ftp.create", f"{username} -> {home_dir}")
    return {**plan, "created": True}


def change_password(username: str, password: str) -> dict:
    if not get_account(username):
        raise FtpError("حساب یافت نشد / account not found")
    _set_password(username, password)
    db.audit(None, "ftp.passwd", username)
    return {"username": username, "password_changed": True}


def set_active(username: str, active: bool) -> dict:
    acc = get_account(username)
    if not acc:
        raise FtpError("حساب یافت نشد / account not found")
    # lock/unlock the system account
    res = oscmd.run_priv(["usermod", "-L" if not active else "-U", username])
    if not res.ok:
        raise FtpError(f"تغییر وضعیت ناموفق / usermod failed: {res.stderr}")
    with db.get_conn() as conn:
        conn.execute("UPDATE ftp_accounts SET active=? WHERE username=?",
                     (1 if active else 0, username))
    db.audit(None, "ftp.active", f"{username}={active}")
    return {"username": username, "active": active}


def delete_account(username: str, remove_home: bool = False, apply: bool = True) -> dict:
    acc = get_account(username)
    if not acc:
        raise FtpError("حساب یافت نشد / account not found")
    if not apply:
        return {"username": username, "applied": False}
    args = ["userdel"]
    if remove_home:
        args.append("-r")
    args.append(username)
    res = oscmd.run_priv(args)
    if not res.ok and "does not exist" not in (res.stderr or ""):
        raise FtpError(f"حذف کاربر ناموفق / userdel failed: {res.stderr or res.stdout}")
    with db.get_conn() as conn:
        conn.execute("DELETE FROM ftp_accounts WHERE username=?", (username,))
    db.audit(None, "ftp.delete", username)
    return {"username": username, "deleted": True}


def get_account(username: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM ftp_accounts WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def list_accounts() -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM ftp_accounts ORDER BY created_at DESC")]


def server_status() -> dict:
    """Report which FTP server (if any) is installed and running."""
    out = {"vsftpd": oscmd.has("vsftpd"), "pure_ftpd": oscmd.has("pure-ftpd")}
    out["installed"] = out["vsftpd"] or out["pure_ftpd"]
    if not out["installed"]:
        out["hint"] = ("هیچ سرور FTP نصب نیست. برای فعال‌سازی: apt install vsftpd "
                       "و chroot_local_user=YES را تنظیم کنید. / No FTP server installed.")
    return out
