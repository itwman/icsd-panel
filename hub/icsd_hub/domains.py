"""Domains and certificates across all servers (phase 4).

Each server's agent reports its nginx sites and certificates (certbot,
Caddy, others). The hub merges them, checks that each domain's DNS really
points at the server that serves it, checks the mail records of each base
domain (MX, SPF, DMARC — no mail server is run, only checked), and raises
an alert when a certificate is close to expiry.

دامنه‌ها و گواهی‌های همهٔ سرورها، بررسی DNS و رکوردهای ایمیل، هشدار انقضا.
"""
from __future__ import annotations

import asyncio
import logging
import time

import dns.asyncresolver
import dns.exception
from fastapi import APIRouter, Depends

from icsd_hub import alerts, auth, db, live, rpc
from icsd_hub.auth import User

log = logging.getLogger("icsd.domains")
router = APIRouter(prefix="/api/domains")
CACHE_TTL = 15 * 60

_cache: dict[str, tuple[float, dict]] = {}          # server -> (time, ssl.list result)
_dns: dict[str, tuple[float, dict]] = {}            # name -> (time, result)

# Second-level suffixes where the registrable domain has three labels.
_SLD = {"ac.ir", "co.ir", "org.ir", "gov.ir", "id.ir", "net.ir", "sch.ir", "co.uk", "org.uk", "com.tr"}


def base_domain(name: str) -> str:
    parts = name.lower().strip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


async def _resolve(name: str, rtype: str) -> list[str]:
    r = dns.asyncresolver.Resolver()
    r.lifetime = 5
    try:
        ans = await r.resolve(name, rtype)
    except (dns.exception.DNSException, OSError):
        return []
    out = []
    for rr in ans:
        if rtype == "TXT":
            out.append(b"".join(rr.strings).decode(errors="replace"))
        elif rtype == "MX":
            out.append(f"{rr.preference} {str(rr.exchange).rstrip('.')}")
        else:
            out.append(rr.to_text())
    return out


async def _dns_info(name: str) -> dict:
    hit = _dns.get(name)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    a, aaaa = await asyncio.gather(_resolve(name, "A"), _resolve(name, "AAAA"))
    res = {"a": a, "aaaa": aaaa}
    _dns[name] = (time.time(), res)
    return res


async def _mail_info(base: str) -> dict:
    key = "mail:" + base
    hit = _dns.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    mx, txt, dmarc = await asyncio.gather(_resolve(base, "MX"), _resolve(base, "TXT"),
                                          _resolve("_dmarc." + base, "TXT"))
    spf = [t for t in txt if t.lower().startswith("v=spf1")]
    dm = [t for t in dmarc if t.lower().startswith("v=dmarc1")]
    res = {"mx": mx, "spf": spf[0] if spf else "", "dmarc": dm[0] if dm else "",
           "spf_multiple": len(spf) > 1}
    _dns[key] = (time.time(), res)
    return res


async def fetch(sid: str, force: bool = False) -> dict | None:
    hit = _cache.get(sid)
    if hit and not force and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    lv = live.peek(sid)
    if not lv or not lv.ws or not (lv.caps or {}).get("ssl"):
        return hit[1] if hit else None
    try:
        data = await rpc.call(sid, "ssl.list", timeout=30)
    except rpc.RPCError as e:
        log.info("ssl.list %s: %s", sid, e)
        return hit[1] if hit else None
    _cache[sid] = (time.time(), data)
    _alert_certs(sid, data)
    return data


def _alert_certs(sid: str, data: dict) -> None:
    now = time.time()
    seen = set()
    for c in data.get("certs") or []:
        subject = f"ssl:{c.get('path') or c.get('name')}"
        seen.add(("cert", subject))
        days = (c.get("not_after", 0) - now) / 86400
        thr = alerts.threshold("cert")
        doms = ", ".join((c.get("domains") or [])[:3])
        alerts.step("cert", sid, subject, days <= thr, round(days, 1),
                    f"گواهی {doms} — {int(days)} روز مانده ({c.get('source')})", now)
    alerts.settle(("cert",), sid, seen, now)


def _cert_for(domain: str, certs: list[dict]) -> dict | None:
    best = None
    for c in certs:
        for d in c.get("domains") or []:
            if d == domain or (d.startswith("*.") and domain.endswith(d[1:]) and domain.count(".") == d.count(".")):
                if best is None or c.get("not_after", 0) > best.get("not_after", 0):
                    best = c
    return best


async def overview(force: bool = False) -> dict:
    servers = db.q("SELECT id, name, last_ip FROM servers WHERE secret_hash IS NOT NULL ORDER BY created")
    results = await asyncio.gather(*(fetch(s["id"], force) for s in servers))
    rows, bases = [], set()
    for srv, data in zip(servers, results):
        if not data:
            continue
        lv = live.peek(srv["id"])
        ip = (lv.ip if lv and lv.ip else srv["last_ip"]) or ""
        certs = data.get("certs") or []
        names = {}
        for site in data.get("sites") or []:
            for n in site.get("names") or []:
                if "*" in n or "." not in n or n.startswith("~"):
                    continue
                names.setdefault(n, site)
        for c in certs:
            if c.get("source") == "caddy":
                for d in c.get("domains") or []:
                    if "*" not in d:
                        names.setdefault(d, {"file": "Caddy", "ssl": True})
        for name, site in sorted(names.items()):
            cert = _cert_for(name, certs)
            rows.append({"domain": name, "server_id": srv["id"], "server": srv["name"], "server_ip": ip,
                         "site": site.get("file"), "ssl": bool(site.get("ssl") or cert),
                         "cert": cert, "certbot": data.get("certbot", False)})
            bases.add(base_domain(name))
    dns_all = await asyncio.gather(*(_dns_info(r["domain"]) for r in rows))
    for r, info in zip(rows, dns_all):
        r["dns"] = info
        ips = info["a"] + info["aaaa"]
        if not ips:
            r["dns_state"] = "none"
        elif r["server_ip"] and r["server_ip"] in ips:
            r["dns_state"] = "ok"
        else:
            r["dns_state"] = "elsewhere"  # another server, or a CDN in front (e.g. ArvanCloud)
    mail = dict(zip(sorted(bases), await asyncio.gather(*(_mail_info(b) for b in sorted(bases)))))
    return {"domains": rows, "mail": mail, "servers": [{"id": s["id"], "name": s["name"]} for s in servers]}


async def refresh_all() -> None:
    """Hourly from the ticker: keeps certificate alerts current."""
    for s in db.q("SELECT id FROM servers WHERE secret_hash IS NOT NULL"):
        try:
            await fetch(s["id"], force=True)
        except Exception:  # noqa: BLE001
            log.exception("refresh %s", s["id"])


@router.get("")
async def get_domains(refresh: bool = False, user: User = Depends(auth.admin)) -> dict:
    if refresh:
        _dns.clear()
    return await overview(force=refresh)


def forget(sid: str) -> None:
    _cache.pop(sid, None)

