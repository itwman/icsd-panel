"""Email management — mail domains, mailboxes, aliases (Postfix/Dovecot provisioning).

مدیریت ایمیل: دامنه‌های ایمیل، صندوق‌ها و الیاس‌ها + تولید نقشه‌های مجازی Postfix/Dovecot.
این ماژول لایهٔ «مدیریت و تأمین» (provisioning) است: داده‌ها را نگه می‌دارد و فایل‌های
نگاشت مجازی را تولید می‌کند. نصب واقعی Postfix/Dovecot روی سرور توسط install/setup انجام
می‌شود (سنگین و اختیاری) و این نگاشت‌ها را مصرف می‌کند.
"""
from __future__ import annotations

import os
import re

from app.core import security, oscmd
from app import db

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")
_DOMAIN_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")

# Where the generated virtual maps are written (consumed by Postfix/Dovecot).
VMAILBOX_MAP = "/etc/postfix/vmailbox"
VALIAS_MAP = "/etc/postfix/virtual"
DOVECOT_USERDB = "/etc/dovecot/users"


class MailError(Exception):
    pass


def _valid_email(addr: str) -> str:
    addr = (addr or "").strip().lower()
    if not _EMAIL_RE.match(addr):
        raise MailError(f"ایمیل نامعتبر / invalid email: {addr!r}")
    return addr


def _valid_domain(d: str) -> str:
    d = (d or "").strip().lower()
    if not _DOMAIN_RE.match(d):
        raise MailError(f"دامنهٔ نامعتبر / invalid domain: {d!r}")
    return d


# ---- Domains ----
def add_domain(domain: str) -> dict:
    domain = _valid_domain(domain)
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM mail_domains WHERE domain=?", (domain,)).fetchone():
            raise MailError("دامنهٔ ایمیل از قبل وجود دارد / mail domain exists")
        conn.execute("INSERT INTO mail_domains (domain) VALUES (?)", (domain,))
    db.audit(None, "mail.add_domain", domain)
    return {"domain": domain, "active": True}


def list_domains() -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM mail_domains ORDER BY domain")]


def delete_domain(domain: str) -> dict:
    domain = _valid_domain(domain)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM mailboxes WHERE domain=?", (domain,))
        conn.execute("DELETE FROM mail_aliases WHERE domain=?", (domain,))
        conn.execute("DELETE FROM mail_domains WHERE domain=?", (domain,))
    db.audit(None, "mail.delete_domain", domain)
    return {"domain": domain, "deleted": True}


# ---- Mailboxes ----
def create_mailbox(address: str, password: str, quota_mb: int = 1024) -> dict:
    address = _valid_email(address)
    if len(password) < 8:
        raise MailError("رمز حداقل ۸ کاراکتر / password >= 8 chars")
    domain = address.split("@", 1)[1]
    with db.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM mail_domains WHERE domain=?", (domain,)).fetchone():
            raise MailError(f"ابتدا دامنهٔ ایمیل {domain} را اضافه کنید / add mail domain first")
        if conn.execute("SELECT 1 FROM mailboxes WHERE address=?", (address,)).fetchone():
            raise MailError("صندوق از قبل وجود دارد / mailbox exists")
        conn.execute(
            "INSERT INTO mailboxes (address, domain, password_hash, quota_mb) VALUES (?,?,?,?)",
            (address, domain, security.hash_password(password), quota_mb),
        )
    db.audit(None, "mail.create_mailbox", address)
    return {"address": address, "quota_mb": quota_mb, "active": True}


def list_mailboxes(domain: str | None = None) -> list[dict]:
    with db.get_conn() as conn:
        if domain:
            rows = conn.execute("SELECT id,address,domain,quota_mb,active,created_at FROM mailboxes WHERE domain=? ORDER BY address", (domain,))
        else:
            rows = conn.execute("SELECT id,address,domain,quota_mb,active,created_at FROM mailboxes ORDER BY address")
        return [dict(r) for r in rows]


def delete_mailbox(address: str) -> dict:
    address = _valid_email(address)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM mailboxes WHERE address=?", (address,))
    db.audit(None, "mail.delete_mailbox", address)
    return {"address": address, "deleted": True}


def change_mailbox_password(address: str, new_password: str) -> dict:
    address = _valid_email(address)
    if len(new_password) < 8:
        raise MailError("رمز حداقل ۸ کاراکتر / password >= 8 chars")
    with db.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM mailboxes WHERE address=?", (address,)).fetchone():
            raise MailError("صندوق یافت نشد / mailbox not found")
        conn.execute("UPDATE mailboxes SET password_hash=? WHERE address=?",
                     (security.hash_password(new_password), address))
    db.audit(None, "mail.change_password", address)
    return {"address": address, "changed": True}


# ---- Aliases ----
def create_alias(source: str, destination: str) -> dict:
    source = _valid_email(source)
    destination = _valid_email(destination)
    domain = source.split("@", 1)[1]
    with db.get_conn() as conn:
        conn.execute("INSERT INTO mail_aliases (source, destination, domain) VALUES (?,?,?)",
                     (source, destination, domain))
    db.audit(None, "mail.create_alias", f"{source} -> {destination}")
    return {"source": source, "destination": destination}


def list_aliases(domain: str | None = None) -> list[dict]:
    with db.get_conn() as conn:
        if domain:
            rows = conn.execute("SELECT * FROM mail_aliases WHERE domain=? ORDER BY source", (domain,))
        else:
            rows = conn.execute("SELECT * FROM mail_aliases ORDER BY source")
        return [dict(r) for r in rows]


def delete_alias(alias_id: int) -> dict:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM mail_aliases WHERE id=?", (alias_id,))
    return {"id": alias_id, "deleted": True}


# ---- Config generation (Postfix/Dovecot virtual maps) ----
def generate_postfix_maps() -> dict:
    """Render Postfix virtual mailbox + alias maps from the database. Pure-ish; testable."""
    mailboxes = list_mailboxes()
    aliases = list_aliases()
    domains = list_domains()

    vmailbox_lines = [f"{m['address']} {m['domain']}/{m['address'].split('@')[0]}/"
                      for m in mailboxes if m["active"]]
    valias_lines = [f"{a['source']} {a['destination']}" for a in aliases]
    vdomains = [d["domain"] for d in domains if d["active"]]

    return {
        "virtual_mailbox_domains": " ".join(vdomains),
        "vmailbox_map": "\n".join(vmailbox_lines) + ("\n" if vmailbox_lines else ""),
        "virtual_alias_map": "\n".join(valias_lines) + ("\n" if valias_lines else ""),
        "files": {VMAILBOX_MAP: "vmailbox_map", VALIAS_MAP: "virtual_alias_map"},
        "note": "این نگاشت‌ها را در Postfix بنویسید و postmap اجرا کنید / write these and run postmap",
    }


# ---- Deliverability: SPF / DKIM / DMARC ----
DKIM_KEY_DIR = "/etc/opendkim/keys"  # private keys land here when writable


def _server_ip() -> str | None:
    r = oscmd.run(["hostname", "-I"], timeout=10)
    if r.ok and r.stdout.strip():
        return r.stdout.split()[0]
    return None


def _generate_dkim_keypair() -> tuple[str, str]:
    """Return (private_pem, public_base64). Uses openssl; raises MailError if absent."""
    if not oscmd.has("openssl"):
        raise MailError("openssl نصب نیست / openssl not installed")
    priv = oscmd.run(["openssl", "genrsa", "2048"], timeout=30)
    if not priv.ok or "PRIVATE KEY" not in priv.stdout:
        raise MailError(f"تولید کلید ناموفق / keygen failed: {priv.stderr[:200]}")
    import subprocess
    pub = subprocess.run(["openssl", "rsa", "-pubout", "-outform", "PEM"],
                         input=priv.stdout, capture_output=True, text=True, timeout=30)
    if pub.returncode != 0:
        raise MailError(f"استخراج کلید عمومی ناموفق / pubkey failed: {pub.stderr[:200]}")
    b64 = "".join(ln for ln in pub.stdout.splitlines() if "-----" not in ln)
    return priv.stdout, b64


def _store_dkim_key(domain: str, selector: str, private_pem: str) -> str | None:
    """Best-effort: save the private key where opendkim expects it."""
    try:
        key_dir = os.path.join(DKIM_KEY_DIR, domain)
        os.makedirs(key_dir, exist_ok=True)
        path = os.path.join(key_dir, f"{selector}.private")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(private_pem)
        os.chmod(path, 0o600)
        return path
    except OSError:
        return None


def dkim_setup(domain: str, selector: str = "icsd") -> dict:
    """Generate a DKIM keypair for a mail domain and persist the public key.

    برای دامنه کلید DKIM می‌سازد؛ کلید عمومی در دیتابیس ذخیره و کلید خصوصی
    (در صورت امکان) در مسیر opendkim نوشته می‌شود.
    """
    domain = _valid_domain(domain)
    if not re.fullmatch(r"[a-z0-9-]{1,32}", selector):
        raise MailError("selector نامعتبر / invalid selector")
    with db.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM mail_domains WHERE domain=?", (domain,)).fetchone():
            raise MailError("ابتدا دامنهٔ ایمیل را اضافه کنید / add the mail domain first")
    private_pem, public_b64 = _generate_dkim_keypair()
    key_path = _store_dkim_key(domain, selector, private_pem)
    from app.services import settings_store
    settings_store.set(f"dkim:{domain}", f"{selector}:{public_b64}")
    db.audit(None, "mail.dkim_setup", f"{domain} selector={selector}")
    out = {"domain": domain, "selector": selector, "public_key": public_b64,
           "key_path": key_path}
    if key_path is None:
        # e.g. running unprivileged or on a dev box — hand the key to the admin once.
        out["private_key"] = private_pem
        out["note"] = ("کلید خصوصی ذخیره نشد؛ آن را در سرور ایمیل قرار دهید / "
                       "private key could not be stored; place it on the mail server")
    return out


def dns_records(domain: str) -> dict:
    """All DNS records a mail domain needs (MX, SPF, DKIM, DMARC).

    رکوردهای DNS لازم برای تحویل درست ایمیل. اگر DKIM تولید شده باشد از
    مقدار ذخیره‌شده استفاده می‌شود.
    """
    domain = _valid_domain(domain)
    ip = _server_ip()
    records = [
        {"name": domain, "type": "MX", "content": f"10 mail.{domain}.",
         "hint": "mail server / سرور ایمیل"},
        {"name": f"mail.{domain}", "type": "A", "content": ip or "<server-ip>",
         "hint": "A record of the mail host / رکورد A میزبان ایمیل"},
        {"name": domain, "type": "TXT",
         "content": f"v=spf1 a mx{(' ip4:' + ip) if ip else ''} ~all",
         "hint": "SPF"},
        {"name": f"_dmarc.{domain}", "type": "TXT",
         "content": f"v=DMARC1; p=quarantine; rua=mailto:postmaster@{domain}",
         "hint": "DMARC"},
    ]
    from app.services import settings_store
    stored = settings_store.get(f"dkim:{domain}")
    if stored and ":" in stored:
        selector, pub = stored.split(":", 1)
        records.append({"name": f"{selector}._domainkey.{domain}", "type": "TXT",
                        "content": f"v=DKIM1; k=rsa; p={pub}", "hint": "DKIM"})
    else:
        records.append({"name": f"icsd._domainkey.{domain}", "type": "TXT",
                        "content": "(اول DKIM تولید کنید / generate DKIM first)",
                        "hint": "DKIM"})
    return {"domain": domain, "records": records}
