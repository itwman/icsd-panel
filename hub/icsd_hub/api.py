"""JSON API for the web UI. Every route below needs a session; changes need admin."""
from __future__ import annotations

import io
import time

import segno
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from icsd_hub import __version__, agents, alerts, auth, checks, config, db, live, metrics, notify
from icsd_hub.auth import User

router = APIRouter(prefix="/api")


def _err(e: auth.AuthError) -> HTTPException:
    return HTTPException(e.status, {"message": str(e), **e.extra})


# ── auth ───────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    otp: str | None = Field(default=None, max_length=10)


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    if request.headers.get("x-icsd") != "1":
        raise HTTPException(403, "درخواست نامعتبر (CSRF)")
    try:
        raw, user = auth.login(body.username, body.password, body.otp,
                               auth.client_ip(request), request.headers.get("user-agent", ""))
    except auth.AuthError as e:
        raise _err(e) from None
    auth.set_cookie(response, raw)
    return _me(user)


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> dict:
    raw = request.cookies.get(auth.COOKIE)
    if raw:
        auth.logout(raw)
    auth.clear_cookie(response)
    return {"ok": True}


def _me(user: User) -> dict:
    return {"username": user.username, "role": user.role, "totp_on": user.totp_on,
            "elevated_until": user.elevated_until,
            "must_setup_2fa": config.settings.require_2fa and not user.totp_on,
            "version": __version__}


@router.get("/me")
async def me(user: User = Depends(auth.current_user_any)) -> dict:
    return _me(user)


class BeginIn(BaseModel):
    code: str | None = Field(default=None, max_length=10)


@router.post("/auth/2fa/begin")
async def twofa_begin(body: BeginIn | None = None, user: User = Depends(auth.current_user_any)) -> dict:
    try:
        data = auth.begin_2fa(user, body.code if body else None)
    except auth.AuthError as e:
        raise _err(e) from None
    buf = io.BytesIO()
    segno.make(data["uri"], error="m").save(buf, kind="svg", scale=5, border=2,
                                            dark="#111", light="#fff", xmldecl=False)
    return {**data, "qr_svg": buf.getvalue().decode()}


class CodeIn(BaseModel):
    code: str = Field(max_length=10)


@router.post("/auth/2fa/confirm")
async def twofa_confirm(body: CodeIn, request: Request, user: User = Depends(auth.current_user_any)) -> dict:
    try:
        auth.confirm_2fa(user, body.code)
    except auth.AuthError as e:
        raise _err(e) from None
    db.audit(user.username, "2fa.enable", "", auth.client_ip(request))
    user.totp_on = True
    return _me(user)


@router.post("/auth/elevate")
async def elevate(body: CodeIn, request: Request, user: User = Depends(auth.admin)) -> dict:
    try:
        until = auth.elevate(user, body.code)
    except auth.AuthError as e:
        raise _err(e) from None
    db.audit(user.username, "elevate", "", auth.client_ip(request))
    return {"until": until}


class PasswordIn(BaseModel):
    old: str = Field(max_length=256)
    new: str = Field(max_length=256)


@router.post("/me/password")
async def change_password(body: PasswordIn, request: Request, response: Response,
                          user: User = Depends(auth.current_user)) -> dict:
    row = db.one("SELECT pw_hash FROM users WHERE id=?", (user.id,))
    from icsd_hub import security
    if not row or not security.verify_password(body.old, row["pw_hash"]):
        raise HTTPException(400, "رمز فعلی نادرست است")
    try:
        auth.set_password(user.id, body.new)
    except auth.AuthError as e:
        raise _err(e) from None
    db.audit(user.username, "password.change", "", auth.client_ip(request))
    auth.clear_cookie(response)
    return {"ok": True, "relogin": True}


# ── overview & servers ─────────────────────────────────────────────────

def _summary(row: dict, now: float) -> dict:
    lv = live.peek(row["id"])
    s = lv.last if lv else {}
    status = "pending" if not row["enrolled"] else (lv.status(now) if lv else "offline")
    disks = s.get("disks") or []
    cont = s.get("containers") or []
    return {
        "id": row["id"], "name": row["name"], "status": status, "host": row["host"],
        "agent_version": (lv.version if lv and lv.version else row["agent_version"]),
        "last_seen": int(lv.last_ts) if lv and lv.last_ts else row["last_seen"],
        "ip": (lv.ip if lv and lv.ip else row["last_ip"]),
        "cpu": s.get("cpu", {}).get("pct"), "mem": s.get("mem", {}).get("pct"),
        "mem_used": s.get("mem", {}).get("used"), "mem_total": s.get("mem", {}).get("total"),
        "swap": s.get("mem", {}).get("swap_pct"),
        "load": s.get("load"), "uptime": s.get("uptime"),
        "rx": s.get("net", {}).get("rx_bps"), "tx": s.get("net", {}).get("tx_bps"),
        "disk": max(disks, key=lambda d: d["pct"]) if disks else None,
        "containers": {"total": len(cont), "running": sum(c["state"] == "running" for c in cont),
                       "bad": sum(c["health"] == "unhealthy" or c["state"] in ("restarting", "dead")
                                  for c in cont)} if cont else None,
        "alerts": alerts.firing_count(row["id"]),
        "spark": metrics.sparkline(row["id"]) if status == "online" else [],
        "enroll_pending": bool(row["enroll_expires"]),
    }


def _latest_agent() -> str:
    f = config.settings.dist_dir / "VERSION"
    return f.read_text().strip() if f.exists() else ""


@router.get("/overview")
async def overview(user: User = Depends(auth.current_user)) -> dict:
    now = time.time()
    servers = [_summary(agents.server_row(r["id"]), now)
               for r in db.q("SELECT id FROM servers ORDER BY created")]
    return {"servers": servers, "alerts": alerts.open_events(), "checks": checks.view(),
            "agent_latest": _latest_agent(), "now": int(now)}


@router.get("/servers/{sid}")
async def server_detail(sid: str, user: User = Depends(auth.current_user)) -> dict:
    row = agents.server_row(sid)
    lv = live.peek(sid)
    return {"server": _summary(row, time.time()), "notes": row["notes"], "created": row["created"],
            "last": lv.last if lv else {}, "agent_latest": _latest_agent(),
            "caps": (lv.caps if lv else {}) or {},
            "events": db.q("SELECT * FROM events WHERE server_id=? ORDER BY started DESC LIMIT 30", (sid,))}


@router.get("/servers/{sid}/series")
async def server_series(sid: str, range: str = "1h", user: User = Depends(auth.current_user)) -> dict:
    agents.server_row(sid)
    return metrics.series(sid, range)


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.post("/servers")
async def add_server(body: ServerIn, request: Request, user: User = Depends(auth.elevated)) -> dict:
    srv, tok = agents.create_server(body.name)
    db.audit(user.username, "server.add", f"{srv['id']} {srv['name']}", auth.client_ip(request))
    return {"server": srv, "command": agents.install_command(agents.base_url(request), tok),
            "expires": srv["enroll_expires"]}


class ServerPatch(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)


@router.patch("/servers/{sid}")
async def edit_server(sid: str, body: ServerPatch, user: User = Depends(auth.admin)) -> dict:
    agents.server_row(sid)
    if body.name is not None and body.name.strip():
        db.x("UPDATE servers SET name=? WHERE id=?", (body.name.strip(), sid))
    if body.notes is not None:
        db.x("UPDATE servers SET notes=? WHERE id=?", (body.notes, sid))
    return agents.server_row(sid)


@router.post("/servers/{sid}/enroll")
async def reenroll(sid: str, request: Request, user: User = Depends(auth.elevated)) -> dict:
    tok = agents.new_enroll_token(sid)
    db.audit(user.username, "server.token", sid, auth.client_ip(request))
    return {"command": agents.install_command(agents.base_url(request), tok),
            "expires": db.now() + agents.ENROLL_TTL}


@router.delete("/servers/{sid}")
async def remove_server(sid: str, request: Request, user: User = Depends(auth.elevated)) -> dict:
    row = agents.server_row(sid)
    await agents.delete_server(sid)
    db.audit(user.username, "server.delete", f"{sid} {row['name']}", auth.client_ip(request))
    return {"ok": True}


# ── alerts ─────────────────────────────────────────────────────────────

@router.get("/alerts")
async def list_alerts(before: int | None = None, user: User = Depends(auth.current_user)) -> dict:
    return {"open": alerts.open_events(), "history": alerts.history(100, before),
            "rules": alerts.rules_view()}


class RuleIn(BaseModel):
    threshold: float
    for_s: int = Field(ge=0, le=86400)
    severity: str
    enabled: bool


@router.put("/rules/{kind}")
async def edit_rule(kind: str, body: RuleIn, request: Request, user: User = Depends(auth.elevated)) -> dict:
    try:
        alerts.update_rule(kind, body.threshold, body.for_s, body.severity, body.enabled)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    db.audit(user.username, "rule.edit", f"{kind} {body.model_dump()}", auth.client_ip(request))
    return {"rules": alerts.rules_view()}


# ── URL checks ─────────────────────────────────────────────────────────

@router.get("/checks")
async def list_checks(user: User = Depends(auth.current_user)) -> dict:
    return {"checks": checks.view()}


@router.get("/checks/{cid}/log")
async def check_log(cid: int, hours: int = 24, user: User = Depends(auth.current_user)) -> dict:
    return checks.log_of(cid, max(1, min(hours, 24 * 30)))


@router.post("/checks")
async def add_check(body: dict, request: Request, user: User = Depends(auth.elevated)) -> dict:
    try:
        c = checks.validate(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from None
    cid = db.x("INSERT INTO checks(name, url, interval, timeout, expect, keyword, enabled, created) "
               "VALUES(?,?,?,?,?,?,?,?)", (*c.values(), db.now()))
    db.audit(user.username, "check.add", c["url"], auth.client_ip(request))
    return {"id": cid, "checks": checks.view()}


@router.put("/checks/{cid}")
async def edit_check(cid: int, body: dict, user: User = Depends(auth.elevated)) -> dict:
    if not db.one("SELECT 1 FROM checks WHERE id=?", (cid,)):
        raise HTTPException(404, "پیدا نشد")
    try:
        c = checks.validate(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from None
    db.x("UPDATE checks SET name=?, url=?, interval=?, timeout=?, expect=?, keyword=?, enabled=? "
         "WHERE id=?", (*c.values(), cid))
    checks.edited(cid, bool(c["enabled"]))
    return {"checks": checks.view()}


@router.delete("/checks/{cid}")
async def remove_check(cid: int, request: Request, user: User = Depends(auth.elevated)) -> dict:
    db.x("DELETE FROM checks WHERE id=?", (cid,))
    db.x("DELETE FROM check_log WHERE check_id=?", (cid,))
    checks.forget(cid)
    db.audit(user.username, "check.delete", str(cid), auth.client_ip(request))
    return {"checks": checks.view()}


# ── settings: notifications ────────────────────────────────────────────

@router.get("/settings/notify")
async def get_notify(user: User = Depends(auth.admin)) -> dict:
    return notify.public_view()


@router.put("/settings/notify")
async def put_notify(body: dict, request: Request, user: User = Depends(auth.elevated)) -> dict:
    try:
        saved = notify.save(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    db.audit(user.username, "notify.edit", "", auth.client_ip(request))
    return saved


@router.post("/settings/notify/test")
async def test_notify(user: User = Depends(auth.admin)) -> dict:
    res = await notify.send(f"✅ پیام آزمایشی ICSD Panel\nاعلان‌ها درست کار می‌کنند.\n🕒 {notify.jalali()}")
    if not res:
        raise HTTPException(400, "هیچ کانالی فعال و کامل نیست")
    return {"results": res}


# ── users & audit ──────────────────────────────────────────────────────

@router.get("/users")
async def users(user: User = Depends(auth.admin)) -> dict:
    return {"users": auth.list_users()}


class UserIn(BaseModel):
    username: str = Field(max_length=32)
    password: str = Field(max_length=256)
    role: str = "viewer"


@router.post("/users")
async def add_user(body: UserIn, request: Request, user: User = Depends(auth.elevated)) -> dict:
    try:
        u = auth.create_user(body.username, body.password, body.role)
    except auth.AuthError as e:
        raise _err(e) from None
    db.audit(user.username, "user.add", f"{u['username']} ({u['role']})", auth.client_ip(request))
    return {"users": auth.list_users()}


@router.delete("/users/{uid}")
async def remove_user(uid: int, request: Request, user: User = Depends(auth.elevated)) -> dict:
    if uid == user.id:
        raise HTTPException(400, "خودتان را نمی‌توانید حذف کنید")
    try:
        auth.delete_user(uid)
    except auth.AuthError as e:
        raise _err(e) from None
    db.audit(user.username, "user.delete", str(uid), auth.client_ip(request))
    return {"users": auth.list_users()}


@router.post("/users/{uid}/reset-2fa")
async def user_reset_2fa(uid: int, request: Request, user: User = Depends(auth.elevated)) -> dict:
    auth.reset_2fa(uid)
    db.audit(user.username, "user.reset_2fa", str(uid), auth.client_ip(request))
    return {"users": auth.list_users()}


@router.get("/audit")
async def audit(user: User = Depends(auth.admin)) -> dict:
    return {"audit": db.q("SELECT * FROM audit ORDER BY id DESC LIMIT 200")}
