"""DNS management — authoritative zone editor (BIND9) + zone-file generator.

مدیریت DNS: ویرایشگر zone مثل cPanel. رکوردها در sqlite نگه‌داری می‌شوند، فایل zone
استاندارد BIND تولید می‌شود، روی BIND محلی اعمال و reload می‌شود؛ و اگر BIND نصب
نباشد، همان فایل zone قابل export است تا در DNS بیرونی (Cloudflare/آروان/…) استفاده شود.

نوشتن فایل‌های /etc/bind با sudo tee و reload با systemctl انجام می‌شود (هر دو در
قوانین sudo نصب‌شده توسط install.sh هستند).
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone

from app.core import oscmd
from app import db

ZONES_DIR = os.environ.get("ICSD_BIND_ZONES_DIR", "/etc/bind/zones")
INCLUDE_FILE = os.environ.get("ICSD_BIND_INCLUDE", "/etc/bind/named.conf.icsd")
NAMED_CONF_LOCAL = os.environ.get("ICSD_BIND_CONF_LOCAL", "/etc/bind/named.conf.local")
BIND_SERVICE = os.environ.get("ICSD_BIND_SERVICE", "bind9")

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)
RECORD_TYPES = {"A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA", "PTR"}
_NAME_RE = re.compile(r"^(@|\*|[A-Za-z0-9_.-]+)$")


class DNSError(Exception):
    pass


def validate_domain(domain: str) -> str:
    domain = (domain or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(domain):
        raise DNSError(f"دامنهٔ نامعتبر / invalid domain: {domain!r}")
    return domain


def bind_installed() -> bool:
    return oscmd.has("named") or oscmd.has("named-checkzone") or os.path.isdir("/etc/bind")


# --------------------------------------------------------------------------- #
# Zones
# --------------------------------------------------------------------------- #
def create_zone(domain: str, ns1: str = "", ns2: str = "", admin_email: str = "",
                ttl: int = 3600, apply: bool = True) -> dict:
    domain = validate_domain(domain)
    if get_zone_by_domain(domain):
        raise DNSError("zone از قبل وجود دارد / zone already exists")
    ns1 = (ns1 or f"ns1.{domain}").strip().rstrip(".")
    ns2 = (ns2 or f"ns2.{domain}").strip().rstrip(".")
    admin_email = (admin_email or f"admin@{domain}").strip()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO dns_zones (domain, ns1, ns2, admin_email, ttl, serial) "
            "VALUES (?,?,?,?,?,?)",
            (domain, ns1, ns2, admin_email, ttl, _new_serial()),
        )
        zid = cur.lastrowid
        # sensible default records
        conn.execute("INSERT INTO dns_records (zone_id, name, type, content, ttl) VALUES (?,?,?,?,?)",
                     (zid, "@", "NS", ns1 + ".", ttl))
        conn.execute("INSERT INTO dns_records (zone_id, name, type, content, ttl) VALUES (?,?,?,?,?)",
                     (zid, "@", "NS", ns2 + ".", ttl))
    db.audit(None, "dns.create_zone", domain)
    zone = get_zone(zid)
    if apply and bind_installed():
        try:
            apply_zone(zid)
        except DNSError:
            pass
    return zone


def list_zones() -> list[dict]:
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM dns_zones ORDER BY domain")]
    for z in rows:
        with db.get_conn() as conn:
            z["record_count"] = conn.execute(
                "SELECT COUNT(*) c FROM dns_records WHERE zone_id=?", (z["id"],)).fetchone()["c"]
    return rows


def get_zone(zone_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM dns_zones WHERE id=?", (zone_id,)).fetchone()
    return dict(row) if row else None


def get_zone_by_domain(domain: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM dns_zones WHERE domain=?", (domain,)).fetchone()
    return dict(row) if row else None


def delete_zone(zone_id: int, apply: bool = True) -> dict:
    zone = get_zone(zone_id)
    if not zone:
        raise DNSError("zone یافت نشد / zone not found")
    with db.get_conn() as conn:
        conn.execute("DELETE FROM dns_records WHERE zone_id=?", (zone_id,))
        conn.execute("DELETE FROM dns_zones WHERE id=?", (zone_id,))
    if apply and bind_installed():
        try:
            _remove_zone_file(zone["domain"])
            _write_include()
            _reload()
        except Exception:  # noqa
            pass
    db.audit(None, "dns.delete_zone", zone["domain"])
    return {"id": zone_id, "deleted": True}


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
def _validate_record(rtype: str, name: str, content: str, priority) -> tuple:
    rtype = (rtype or "").upper().strip()
    if rtype not in RECORD_TYPES:
        raise DNSError(f"نوع رکورد نامعتبر / invalid record type: {rtype}")
    name = (name or "@").strip()
    if not _NAME_RE.match(name):
        raise DNSError(f"نام رکورد نامعتبر / invalid record name: {name!r}")
    content = (content or "").strip()
    if not content or "\n" in content or "\r" in content:
        raise DNSError("محتوای رکورد نامعتبر / invalid record content")
    if rtype in ("MX", "SRV"):
        try:
            priority = int(priority if priority not in (None, "") else 10)
        except (TypeError, ValueError):
            raise DNSError("priority باید عدد باشد / priority must be a number")
    else:
        priority = None
    return rtype, name, content, priority


def add_record(zone_id: int, rtype: str, name: str, content: str,
               ttl: int = 3600, priority=None, apply: bool = True) -> dict:
    if not get_zone(zone_id):
        raise DNSError("zone یافت نشد / zone not found")
    rtype, name, content, priority = _validate_record(rtype, name, content, priority)
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO dns_records (zone_id, name, type, content, ttl, priority) "
            "VALUES (?,?,?,?,?,?)",
            (zone_id, name, rtype, content, ttl or 3600, priority),
        )
        rid = cur.lastrowid
    _bump_serial(zone_id)
    if apply and bind_installed():
        apply_zone(zone_id)
    db.audit(None, "dns.add_record", f"{rtype} {name} -> {content}")
    return {"id": rid, "zone_id": zone_id, "type": rtype, "name": name,
            "content": content, "ttl": ttl or 3600, "priority": priority}


def update_record(record_id: int, rtype: str, name: str, content: str,
                  ttl: int = 3600, priority=None, apply: bool = True) -> dict:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM dns_records WHERE id=?", (record_id,)).fetchone()
    if not row:
        raise DNSError("رکورد یافت نشد / record not found")
    zone_id = row["zone_id"]
    rtype, name, content, priority = _validate_record(rtype, name, content, priority)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE dns_records SET name=?, type=?, content=?, ttl=?, priority=? WHERE id=?",
            (name, rtype, content, ttl or 3600, priority, record_id),
        )
    _bump_serial(zone_id)
    if apply and bind_installed():
        apply_zone(zone_id)
    db.audit(None, "dns.update_record", f"{rtype} {name}")
    return {"id": record_id, "updated": True}


def delete_record(record_id: int, apply: bool = True) -> dict:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM dns_records WHERE id=?", (record_id,)).fetchone()
    if not row:
        raise DNSError("رکورد یافت نشد / record not found")
    zone_id = row["zone_id"]
    with db.get_conn() as conn:
        conn.execute("DELETE FROM dns_records WHERE id=?", (record_id,))
    _bump_serial(zone_id)
    if apply and bind_installed():
        apply_zone(zone_id)
    db.audit(None, "dns.delete_record", str(record_id))
    return {"id": record_id, "deleted": True}


def list_records(zone_id: int) -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM dns_records WHERE zone_id=? ORDER BY type, name", (zone_id,))]


# --------------------------------------------------------------------------- #
# Zone-file generation (pure) + apply
# --------------------------------------------------------------------------- #
def _new_serial() -> int:
    return int(datetime.now(timezone.utc).strftime("%Y%m%d")) * 100 + 1


def _bump_serial(zone_id: int) -> None:
    z = get_zone(zone_id)
    if not z:
        return
    today = int(datetime.now(timezone.utc).strftime("%Y%m%d")) * 100
    serial = z["serial"] or 0
    serial = max(serial + 1, today + 1)
    with db.get_conn() as conn:
        conn.execute("UPDATE dns_zones SET serial=? WHERE id=?", (serial, zone_id))


def generate_zonefile(zone: dict, records: list[dict]) -> str:
    """Render a standard BIND zone file. Pure function — testable."""
    domain = zone["domain"]
    ttl = zone.get("ttl") or 3600
    ns1 = (zone.get("ns1") or f"ns1.{domain}").rstrip(".")
    admin = (zone.get("admin_email") or f"admin@{domain}").replace("@", ".", 1).rstrip(".")
    serial = zone.get("serial") or _new_serial()
    lines = [
        f"$TTL {ttl}",
        f"@\tIN\tSOA\t{ns1}. {admin}. (",
        f"\t\t{serial}\t; serial",
        "\t\t3600\t\t; refresh",
        "\t\t1800\t\t; retry",
        "\t\t1209600\t; expire",
        "\t\t86400 )\t; minimum",
        "",
    ]
    for r in records:
        name = r["name"] or "@"
        rtype = r["type"]
        rttl = r.get("ttl") or ttl
        content = r["content"]
        if rtype == "TXT" and not content.startswith('"'):
            content = '"' + content.replace('"', '\\"') + '"'
        if rtype in ("MX", "SRV"):
            prio = r.get("priority") if r.get("priority") is not None else 10
            lines.append(f"{name}\t{rttl}\tIN\t{rtype}\t{prio} {content}")
        else:
            lines.append(f"{name}\t{rttl}\tIN\t{rtype}\t{content}")
    return "\n".join(lines) + "\n"


def export_zonefile(zone_id: int) -> str:
    zone = get_zone(zone_id)
    if not zone:
        raise DNSError("zone یافت نشد / zone not found")
    return generate_zonefile(zone, list_records(zone_id))


def _sudo_write(path: str, content: str) -> None:
    proc = subprocess.run(oscmd.sudo_prefix() + ["tee", path], input=content,
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise DNSError(f"نوشتن فایل ناموفق / write failed ({path}): {proc.stderr[:160]}")


def _zone_file_path(domain: str) -> str:
    return os.path.join(ZONES_DIR, f"db.{domain}")


def _remove_zone_file(domain: str) -> None:
    oscmd.run_priv(["rm", "-f", _zone_file_path(domain)])


def _write_include() -> None:
    """Regenerate the managed include listing all zones, and ensure it's included."""
    blocks = []
    for z in list_zones():
        zf = _zone_file_path(z["domain"])
        blocks.append(f'zone "{z["domain"]}" {{\n    type master;\n    file "{zf}";\n}};')
    _sudo_write(INCLUDE_FILE, "\n".join(blocks) + "\n")
    # ensure named.conf.local includes our file (idempotent)
    if os.path.isfile(NAMED_CONF_LOCAL):
        try:
            current = open(NAMED_CONF_LOCAL, encoding="utf-8", errors="ignore").read()
        except OSError:
            current = ""
        if INCLUDE_FILE not in current:
            _sudo_write(NAMED_CONF_LOCAL, current + f'\ninclude "{INCLUDE_FILE}";\n')


def _reload() -> None:
    oscmd.run_priv(["systemctl", "reload", BIND_SERVICE])


def apply_zone(zone_id: int) -> dict:
    """Write the zone file + include, validate (best effort), and reload BIND."""
    zone = get_zone(zone_id)
    if not zone:
        raise DNSError("zone یافت نشد / zone not found")
    if not bind_installed():
        raise DNSError("BIND نصب نیست — می‌توانید zone را export کنید / BIND not installed; use export")
    oscmd.run_priv(["mkdir", "-p", ZONES_DIR])
    zf = _zone_file_path(zone["domain"])
    _sudo_write(zf, generate_zonefile(zone, list_records(zone_id)))
    _write_include()
    # best-effort validation
    if oscmd.has("named-checkzone"):
        chk = oscmd.run_priv(["named-checkzone", zone["domain"], zf])
        if not chk.ok:
            raise DNSError(f"zone نامعتبر / invalid zone: {chk.stdout or chk.stderr}")
    _reload()
    db.audit(None, "dns.apply", zone["domain"])
    return {"domain": zone["domain"], "applied": True, "file": zf}


def status() -> dict:
    return {"bind_installed": bind_installed(), "zones_dir": ZONES_DIR,
            "service": BIND_SERVICE,
            "hint": None if bind_installed() else
            "BIND نصب نیست؛ رکوردها ذخیره و قابل export هستند. برای DNS محلی: apt install bind9"}
