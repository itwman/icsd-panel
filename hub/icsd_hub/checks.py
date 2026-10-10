"""URL checks from the hub: is the site up, how fast, when does its SSL expire.

This is what would have caught «فرش پلاس خطای 500 میده» before a user did.
A 5xx counts as down even though the server answered.

پایشِ آدرس‌ها از سمتِ hub: بالا بودن، زمان پاسخ و انقضای گواهی.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from urllib.parse import urlparse

import httpx

from icsd_hub import alerts, db

log = logging.getLogger("icsd.checks")
CERT_EVERY = 6 * 3600

_state: dict[int, dict] = {}  # id -> {next, fails, last..., cert_days, cert_at}


def validate(data: dict) -> dict:
    url = str(data.get("url", "")).strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("آدرس باید با http:// یا https:// شروع شود")
    expect = str(data.get("expect", "2xx,3xx")).replace(" ", "") or "2xx,3xx"
    for part in expect.split(","):
        if not (len(part) == 3 and (part.isdigit() or (part[0] in "12345" and part[1:] == "xx"))):
            raise ValueError("کد مورد انتظار مثل 200 یا 2xx,3xx")
    return {
        "name": (str(data.get("name") or u.hostname)).strip()[:80],
        "url": url[:500],
        "interval": max(30, min(int(data.get("interval") or 60), 3600)),
        "timeout": max(2, min(int(data.get("timeout") or 10), 60)),
        "expect": expect,
        "keyword": str(data.get("keyword") or "")[:100],
        "enabled": int(bool(data.get("enabled", True))),
    }


def _status_ok(code: int, expect: str) -> bool:
    for part in expect.split(","):
        if part.endswith("xx") and str(code)[0] == part[0]:
            return True
        if part == str(code):
            return True
    return False


async def cert_days(url: str, timeout: int = 10) -> float | None:
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        return None
    ctx = ssl.create_default_context()
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(u.hostname, u.port or 443, ssl=ctx, server_hostname=u.hostname),
        timeout)
    try:
        cert = writer.get_extra_info("peercert")
        return round((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400, 1)
    finally:
        writer.close()


async def probe(client: httpx.AsyncClient, chk: dict) -> dict:
    t0 = time.perf_counter()
    try:
        r = await client.get(chk["url"], timeout=chk["timeout"], follow_redirects=True)
        ms = int((time.perf_counter() - t0) * 1000)
        ok = _status_ok(r.status_code, chk["expect"])
        err = "" if ok else f"HTTP {r.status_code}"
        if ok and chk["keyword"] and chk["keyword"] not in r.text:
            ok, err = False, f"عبارت «{chk['keyword']}» در صفحه نیست"
        return {"ok": ok, "ms": ms, "code": r.status_code, "error": err}
    except httpx.TimeoutException:
        return {"ok": False, "ms": None, "code": None, "error": f"بدون پاسخ تا {chk['timeout']} ثانیه"}
    except httpx.ConnectError as e:
        msg = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            return {"ok": False, "ms": None, "code": None,
                    "error": "گواهی SSL نامعتبر است: " + msg.split("CERTIFICATE_VERIFY_FAILED")[-1].strip(" ]:()")[:200]}
        if "Name or service not known" in msg or "nodename nor servname" in msg or "getaddrinfo" in msg:
            return {"ok": False, "ms": None, "code": None, "error": "نام دامنه پیدا نشد (DNS)"}
        return {"ok": False, "ms": None, "code": None, "error": "اتصال برقرار نشد (سرور خاموش است یا پورت بسته است)"}
    except httpx.HTTPError as e:
        msg = str(e) or e.__class__.__name__
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            msg = "گواهی SSL نامعتبر است: " + msg.split("CERTIFICATE_VERIFY_FAILED")[-1].strip(" ]:()")
        return {"ok": False, "ms": None, "code": None, "error": msg[:300]}


async def run_due(client: httpx.AsyncClient, now: float | None = None) -> None:
    now = now or time.time()
    due = [c for c in db.q("SELECT * FROM checks WHERE enabled=1")
           if _state.get(c["id"], {}).get("next", 0) <= now]
    if not due:
        return
    results = await asyncio.gather(*(probe(client, c) for c in due))
    for chk, res in zip(due, results):
        if chk["id"] not in _state:
            # After a hub restart an open alert means «it was already failing»:
            # start from the threshold so one more failure does not look like a recovery.
            down = alerts.is_open("check", "", str(chk["id"]))
            _state[chk["id"]] = {"fails": int(alerts.threshold("check")) if down else 0,
                                 "cert_at": 0, "cert_days": None}
        st = _state[chk["id"]]
        st["next"] = now + chk["interval"]
        st["fails"] = 0 if res["ok"] else st["fails"] + 1
        st.update(last_at=int(now), last_ok=res["ok"], last_ms=res["ms"],
                  last_code=res["code"], last_error=res["error"])
        if chk["url"].startswith("https://") and now - st["cert_at"] > CERT_EVERY:
            st["cert_at"] = now
            try:
                st["cert_days"] = await cert_days(chk["url"], chk["timeout"])
            except Exception as e:  # noqa: BLE001 — the probe already reports reachability
                log.info("cert check %s: %s", chk["url"], e)
        db.x("INSERT OR REPLACE INTO check_log(check_id, ts, ok, ms, code, error) VALUES(?,?,?,?,?,?)",
             (chk["id"], int(now), int(res["ok"]), res["ms"], res["code"], res["error"]))
        alerts.evaluate_check(chk["id"], chk["name"], chk["url"], st["fails"], res["error"],
                              st.get("cert_days"), now)


def forget(check_id: int) -> None:
    """Deleted or disabled: close its alerts without a misleading «resolved»."""
    _state.pop(check_id, None)
    alerts.step("check", "", str(check_id), False, 0, "", quiet=True)
    alerts.step("cert", "", str(check_id), False, 0, "", quiet=True)


def edited(check_id: int, enabled: bool) -> None:
    if not enabled:
        forget(check_id)
    elif check_id in _state:
        _state[check_id]["next"] = 0      # probe again right away, keep the fail count
        _state[check_id]["cert_at"] = 0


def view() -> list[dict]:
    since = int(time.time()) - 86400
    up = {r["check_id"]: r for r in db.q(
        "SELECT check_id, AVG(ok)*100 AS uptime, AVG(ms) AS avg_ms FROM check_log WHERE ts>=? "
        "GROUP BY check_id", (since,))}
    out = []
    for c in db.q("SELECT * FROM checks ORDER BY id"):
        st = _state.get(c["id"], {})
        u = up.get(c["id"], {})
        out.append({**c, "enabled": bool(c["enabled"]),
                    "last_at": st.get("last_at"), "ok": st.get("last_ok"), "ms": st.get("last_ms"),
                    "code": st.get("last_code"), "error": st.get("last_error", ""),
                    "fails": st.get("fails", 0), "cert_days": st.get("cert_days"),
                    "uptime_24h": round(u["uptime"], 2) if u.get("uptime") is not None else None,
                    "avg_ms_24h": int(u["avg_ms"]) if u.get("avg_ms") is not None else None})
    return out


def log_of(check_id: int, hours: int = 24) -> dict:
    rows = db.q("SELECT ts, ok, ms, code, error FROM check_log WHERE check_id=? AND ts>=? ORDER BY ts",
                (check_id, int(time.time()) - hours * 3600))
    return {"t": [r["ts"] for r in rows], "ms": [r["ms"] for r in rows],
            "ok": [r["ok"] for r in rows],
            "errors": [{"ts": r["ts"], "error": r["error"], "code": r["code"]} for r in rows if not r["ok"]][-50:]}
