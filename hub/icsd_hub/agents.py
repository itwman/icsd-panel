"""The agent side of the hub: enrollment and the long-lived WebSocket.

Enrollment: the panel shows a one-time token (valid 24 h). The agent trades
it for a permanent secret; the hub keeps only the secret's SHA-256. A stolen
database therefore does not let anyone impersonate an agent, and the token
in the install command is worthless once used.

ثبت‌نام: توکنِ یک‌بارمصرف با یک رمزِ دائمی عوض می‌شود؛ hub فقط هشِ رمز را نگه می‌دارد.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from icsd_hub import alerts, auth, config, db, live, rpc, security

log = logging.getLogger("icsd.agents")
router = APIRouter()

CLOSE_UNAUTHORIZED = 4401
CLOSE_REPLACED = 4000
MAX_MESSAGE = 4 * 1024 * 1024  # file chunks come back base64-encoded
ENROLL_TTL = 24 * 3600


# ── servers ────────────────────────────────────────────────────────────

def new_server_id() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "s" + "".join(secrets.choice(alphabet) for _ in range(7))


def create_server(name: str) -> tuple[dict, str]:
    name = (name or "").strip()[:60]
    if not name:
        raise HTTPException(400, "نام سرور لازم است")
    sid = new_server_id()
    tok = security.token(24)
    db.x("INSERT INTO servers(id, name, created, enroll_hash, enroll_expires) VALUES(?,?,?,?,?)",
         (sid, name, db.now(), security.sha256(tok), db.now() + ENROLL_TTL))
    return server_row(sid), tok


def new_enroll_token(sid: str) -> str:
    if not db.one("SELECT 1 FROM servers WHERE id=?", (sid,)):
        raise HTTPException(404, "سرور پیدا نشد")
    tok = security.token(24)
    db.x("UPDATE servers SET enroll_hash=?, enroll_expires=? WHERE id=?",
         (security.sha256(tok), db.now() + ENROLL_TTL, sid))
    return tok


def server_row(sid: str) -> dict:
    row = db.one("SELECT id, name, created, host_json, agent_version, last_seen, last_ip, notes, "
                 "secret_hash IS NOT NULL AS enrolled, enroll_expires FROM servers WHERE id=?", (sid,))
    if not row:
        raise HTTPException(404, "سرور پیدا نشد")
    row["host"] = json.loads(row.pop("host_json") or "{}")
    row["enrolled"] = bool(row["enrolled"])
    if row["enroll_expires"] and row["enroll_expires"] < db.now():
        row["enroll_expires"] = None
    return row


async def delete_server(sid: str) -> None:
    lv = live.peek(sid)
    if lv and lv.ws is not None:
        try:
            await lv.ws.close(code=CLOSE_UNAUTHORIZED, reason="server deleted")
        except Exception:  # noqa: BLE001
            pass
    live.forget(sid)
    alerts.forget_server(sid)
    from icsd_hub import domains
    domains.forget(sid)
    for table in ("samples_1m", "samples_1h"):
        db.x(f"DELETE FROM {table} WHERE server_id=?", (sid,))
    db.x("DELETE FROM servers WHERE id=?", (sid,))


def install_command(base: str, token: str) -> str:
    return f"curl -fsSL {base}/install.sh | sudo bash -s -- --token {token}"


def base_url(request: Request) -> str:
    return config.settings.public_url or str(request.base_url).rstrip("/")


# ── enrollment ─────────────────────────────────────────────────────────

class EnrollIn(BaseModel):
    token: str
    host: dict = {}
    version: str = ""


@router.post("/api/agent/enroll")
async def enroll(body: EnrollIn, request: Request) -> dict:
    ip = auth.client_ip(request)
    if db.one("SELECT COUNT(*) AS n FROM login_fail WHERE ip=? AND username='~enroll' AND ts>?",
              (ip, db.now() - 900))["n"] >= 10:
        raise HTTPException(429, "too many attempts")
    row = db.one("SELECT id, enroll_expires FROM servers WHERE enroll_hash=?",
                 (security.sha256(body.token.strip()),))
    if not row or (row["enroll_expires"] or 0) < db.now():
        db.x("INSERT INTO login_fail(ip, username, ts) VALUES(?,?,?)", (ip, "~enroll", db.now()))
        raise HTTPException(403, "توکن نامعتبر یا منقضی است؛ از پنل یک دستور نصب تازه بگیرید")
    secret = security.token(32)
    host = {k: v for k, v in (body.host or {}).items() if isinstance(v, (str, int, float))}
    db.x("UPDATE servers SET secret_hash=?, enroll_hash=NULL, enroll_expires=NULL, host_json=?, "
         "agent_version=?, last_ip=? WHERE id=?",
         (security.sha256(secret), json.dumps(host)[:4000], body.version[:20], ip, row["id"]))
    # A re-install replaces the old identity: drop the old connection.
    lv = live.peek(row["id"])
    if lv and lv.ws is not None:
        try:
            await lv.ws.close(code=CLOSE_UNAUTHORIZED, reason="re-enrolled")
        except Exception:  # noqa: BLE001
            pass
    db.audit(None, "agent.enroll", f"{row['id']} {host.get('hostname', '')}", ip)
    return {"server_id": row["id"], "secret": secret, "interval": config.settings.sample_interval}


# ── the agent socket ───────────────────────────────────────────────────

def _authenticate(header: str) -> str | None:
    if not header.startswith("Bearer ") or ":" not in header:
        return None
    sid, _, secret = header[7:].strip().partition(":")
    row = db.one("SELECT secret_hash FROM servers WHERE id=?", (sid[:16],))
    if not row or not row["secret_hash"]:
        security.same(security.sha256(secret), "0" * 64)  # same work either way
        return None
    return sid if security.same(security.sha256(secret), row["secret_hash"]) else None


@router.websocket("/api/agent/ws")
async def agent_ws(ws: WebSocket) -> None:
    await ws.accept()
    sid = _authenticate(ws.headers.get("authorization", ""))
    if not sid:
        await ws.close(code=CLOSE_UNAUTHORIZED, reason="unauthorized")
        return
    lv = live.get(sid)
    if lv.ws is not None:
        old = lv.ws
        lv.ws = None
        try:
            await old.close(code=CLOSE_REPLACED, reason="replaced by a newer connection")
        except Exception:  # noqa: BLE001
            pass
    ip = ws.client.host if ws.client else ""
    lv.ws, lv.ip, lv.connected_at = ws, ip, time.time()
    lv.version = ws.headers.get("x-agent-version", "")[:20]
    db.x("UPDATE servers SET last_ip=?, agent_version=? WHERE id=?", (ip, lv.version, sid))
    log.info("agent %s connected from %s (v%s)", sid, ip, lv.version)
    await ws.send_text(json.dumps({"t": "cfg", "interval": config.settings.sample_interval}))
    host = json.loads((db.one("SELECT host_json FROM servers WHERE id=?", (sid,)) or {}).get("host_json") or "{}")
    try:
        while True:
            text = await ws.receive_text()
            if len(text) > MAX_MESSAGE:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            kind = msg.get("t") if isinstance(msg, dict) else None
            if kind == "m" and isinstance(msg.get("d"), dict):
                s = live.ingest(lv, msg["d"])
                alerts.evaluate_sample(sid, s, int(host.get("cpus") or 1))
            elif kind == "res":
                fut = lv.pending.pop(str(msg.get("id", "")), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
            elif kind == "hello" and isinstance(msg.get("host"), dict):
                host = {k: v for k, v in msg["host"].items() if isinstance(v, (str, int, float))}
                db.x("UPDATE servers SET host_json=? WHERE id=?", (json.dumps(host)[:4000], sid))
                caps = msg.get("caps")
                lv.caps = caps if isinstance(caps, dict) else {"mode": "monitor"}
            await asyncio.sleep(0)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("agent %s: %s", sid, e)
    finally:
        if lv.ws is ws:
            lv.ws = None
            for fut in lv.pending.values():
                if not fut.done():
                    fut.set_exception(rpc.RPCError("ارتباط با agent قطع شد", 503))
            lv.pending.clear()
            if live.peek(sid) is lv and db.one("SELECT 1 FROM servers WHERE id=?", (sid,)):
                live.flush(lv)
                db.x("UPDATE servers SET last_seen=? WHERE id=?", (int(lv.last_ts or time.time()), sid))
            log.info("agent %s disconnected", sid)
