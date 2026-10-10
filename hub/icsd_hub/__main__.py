"""Command line for the hub (inside the container: `icsd-hub ...`).

    icsd-hub create-admin [USERNAME]     first account (asks for the password)
    icsd-hub set-password USERNAME
    icsd-hub reset-2fa USERNAME          lost phone
    icsd-hub users
"""
from __future__ import annotations

import getpass
import sys

from icsd_hub import auth, db


def _password() -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    p1 = getpass.getpass("رمز (password): ")
    p2 = getpass.getpass("تکرار رمز (again): ")
    if p1 != p2:
        sys.exit("رمزها یکسان نیستند / passwords differ")
    return p1


def _user(name: str) -> dict:
    row = db.one("SELECT id, username FROM users WHERE username=?", (name.strip().lower(),))
    if not row:
        sys.exit(f"کاربر {name} پیدا نشد / no such user")
    return row


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "create-admin":
            name = args[0] if args else input("نام کاربری (username) [admin]: ").strip() or "admin"
            u = auth.create_user(name, _password(), "admin")
            print(f"✔ مدیر «{u['username']}» ساخته شد. پس از اولین ورود، رمز دوم (2FA) را فعال کنید.")
        elif cmd == "set-password" and args:
            auth.set_password(_user(args[0])["id"], _password())
            print("✔ رمز عوض شد و همهٔ نشست‌های این کاربر بسته شد.")
        elif cmd == "reset-2fa" and args:
            auth.reset_2fa(_user(args[0])["id"])
            print("✔ رمز دوم غیرفعال شد؛ کاربر در ورود بعدی باید دوباره فعالش کند.")
        elif cmd == "users":
            for u in auth.list_users():
                print(f"{u['id']:>3}  {u['username']:<20} {u['role']:<7} 2FA={'on' if u['totp_on'] else 'off'}")
        else:
            sys.exit(__doc__)
    except auth.AuthError as e:
        sys.exit(f"✘ {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
