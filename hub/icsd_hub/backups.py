"""Scheduled backups (phase 3).

A plan says: on server S, dump database D (or archive folder F) every N
hours, keep the last K copies there, and optionally copy each one to
server T (keeping K' copies there) and delete the local one — which is what
keeps a small disk from filling up with backups, the cause of the 500s.

The agent does the work on its own server (with a free-space guard); the hub
schedules, relays copies between servers chunk by chunk, prunes and alerts.

بک‌آپ زمان‌بندی‌شده: dump دیتابیس یا آرشیو پوشه، نگه‌داری K نسخه، کپی به سرور دیگر.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import posixpath
import secrets
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from icsd_hub import agents, alerts, auth, db, live, notify, rpc
from icsd_hub.auth import User

log = logging.getLogger("icsd.backups")
router = APIRouter(prefix="/api/backups")
CHUNK = 512 * 1024
MAX_PARALLEL = 2

_running: dict[int, asyncio.Task] = {}


# ── schedule ───────────────────────────────────────────────────────────

def next_run(every_hours: int, start_hour: int, after: float | None = None) -> int:
    """The first time ≥ after that is start_hour (Tehran) + k * every_hours."""
    after = after or time.time()
    tz = notify.TEHRAN
    now = datetime.fromtimestamp(after, tz)
    anchor = now.replace(hour=start_hour % 24, minute=0, second=0, microsecond=0)
    if anchor > now:
        anchor -= timedelta(days=1)
    step = timedelta(hours=max(1, every_hours))
    t = anchor
    while t.timestamp() <= after:
        t += step
    return int(t.timestamp())


def plans() -> list[dict]:
    rows = db.q("SELECT p.*, s.name AS server_name, d.name AS dest_name FROM backup_plans p "
                "JOIN servers s ON s.id=p.server_id LEFT JOIN servers d ON d.id=p.dest_server ORDER BY p.id")
    last = {r["plan_id"]: r for r in db.q(
        "SELECT r.* FROM backup_runs r JOIN (SELECT plan_id, MAX(id) AS id FROM backup_runs GROUP BY plan_id) m "
        "ON m.id=r.id")}
    for p in rows:
        p["target"] = json.loads(p["target"])
        p["enabled"] = bool(p["enabled"])
        p["delete_local"] = bool(p["delete_local"])
        p["last"] = last.get(p["id"])
        p["running"] = p["id"] in _running
    return rows


def runs(limit: int = 100) -> list[dict]:
    return db.q("SELECT r.*, p.name AS plan_name, s.name AS server_name, d.name AS dest_name "
                "FROM backup_runs r LEFT JOIN backup_plans p ON p.id=r.plan_id "
                "LEFT JOIN servers s ON s.id=r.server_id LEFT JOIN servers d ON d.id=r.dest_server "
                "ORDER BY r.id DESC LIMIT ?", (min(limit, 500),))


def recover() -> None:
    """A hub restart interrupts running copies: mark them, they'll run again."""
    db.x("UPDATE backup_runs SET state='error', ended=?, error='پنل در میانهٔ کار دوباره راه‌اندازی شد' "
         "WHERE state='running'", (db.now(),))


def tick() -> None:
    """Start due plans (called every 15 s by the hub's ticker)."""
    now = time.time()
    for p in db.q("SELECT * FROM backup_plans WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?", (now,)):
        if p["id"] in _running or len(_running) >= MAX_PARALLEL:
            continue
        lv = live.peek(p["server_id"])
        if not lv or not lv.ws:
            continue  # server offline: try again on a later tick
        db.x("UPDATE backup_plans SET next_run=? WHERE id=?",
             (next_run(p["every_hours"], p["start_hour"], now), p["id"]))
        start(p["id"], "schedule")


def start(plan_id: int, trigger: str) -> int:
    if plan_id in _running:
        raise HTTPException(409, "این برنامه همین الان در حال اجراست")
    plan = db.one("SELECT * FROM backup_plans WHERE id=?", (plan_id,))
    if not plan:
        raise HTTPException(404, "برنامه پیدا نشد")
    run_id = db.x("INSERT INTO backup_runs(plan_id, server_id, started, state, dest_server, trigger) "
                  "VALUES(?,?,?,?,?,?)", (plan_id, plan["server_id"], db.now(), "running",
                                          plan["dest_server"], trigger))
    task = asyncio.get_running_loop().create_task(_run(plan, run_id))
    _running[plan_id] = task
    task.add_done_callback(lambda _t: _running.pop(plan_id, None))
    return run_id


# ── one run ────────────────────────────────────────────────────────────

async def _run(plan: dict, run_id: int) -> None:
    sid = plan["server_id"]
    prefix = plan.get("prefix") or f"p{plan['id']}"
    target = json.loads(plan["target"])
    try:
        args = {"kind": plan["kind"], "prefix": prefix}
        if plan["kind"] == "db":
            args["db"] = target["db"]
        else:
            args["path"] = target["path"]
            args["exclude"] = target.get("exclude", [])
        started = await rpc.call(sid, "backup.run", args, timeout=120)
        job = await rpc.wait_job(sid, started["job"])
        if job.get("state") != "done":
            raise rpc.RPCError(job.get("error") or "بک‌آپ ناموفق بود")
        path, size = job["result"]["path"], job["result"]["size"]
        db.x("UPDATE backup_runs SET path=?, size=? WHERE id=?", (path, size, run_id))
        await rpc.call(sid, "backup.prune", {"dir": "", "prefix": prefix, "keep": plan["keep"]})

        dest_path = None
        if plan["dest_server"]:
            dest_path = await copy_between(sid, path, plan["dest_server"], size)
            db.x("UPDATE backup_runs SET dest_path=? WHERE id=?", (dest_path, run_id))
            await rpc.call(plan["dest_server"], "backup.prune",
                           {"dir": f"remote/{sid}", "prefix": prefix, "keep": plan["dest_keep"]})
            if plan["delete_local"]:
                await rpc.call(sid, "fs.delete", {"paths": [path]})
        db.x("UPDATE backup_runs SET state='done', ended=? WHERE id=?", (db.now(), run_id))
        alerts.step("backup", sid, f"plan:{plan['id']}", False, 0, "")
    except Exception as e:  # noqa: BLE001 — every failure must be recorded and alerted
        msg = str(e) or e.__class__.__name__
        log.warning("backup plan %s failed: %s", plan["id"], msg)
        db.x("UPDATE backup_runs SET state='error', ended=?, error=? WHERE id=?",
             (db.now(), msg[:1000], run_id))
        alerts.step("backup", sid, f"plan:{plan['id']}", True, 1,
                    f"بک‌آپ «{plan['name']}» ناموفق: {msg[:300]}")


async def copy_between(src: str, path: str, dst: str, size: int) -> str:
    """Stream a file from one server to another through the hub, in chunks."""
    dlv = live.peek(dst)
    if not dlv or not dlv.ws:
        raise rpc.RPCError("سرور مقصد به پنل وصل نیست")
    backup_dir = (dlv.caps or {}).get("backup_dir") or "/var/backups/icsd"
    dest_dir = posixpath.join(backup_dir, "remote", src)
    await rpc.call(dst, "fs.mkdir", {"path": dest_dir, "parents": True})
    dest = posixpath.join(dest_dir, posixpath.basename(path))
    offset = 0
    upload = ""
    try:
        while True:
            part = await rpc.call(src, "fs.read", {"path": path, "offset": offset, "length": CHUNK}, timeout=120)
            data = base64.b64decode(part["data"])
            final = bool(part["eof"]) or not data
            res = await rpc.call(dst, "fs.write", {"path": dest, "data": part["data"], "offset": offset,
                                                   "final": final, "upload": upload}, timeout=120)
            upload = (res or {}).get("upload", upload)
            offset += len(data)
            if final:
                break
    except Exception:
        if upload:  # don't leave a half-copied file filling the destination's disk
            try:
                await rpc.call(dst, "fs.abort", {"path": dest, "upload": upload}, timeout=30)
            except rpc.RPCError:
                pass
        raise
    if offset != size:
        raise rpc.RPCError(f"کپی ناقص: {offset} از {size} بایت")
    return dest


# ── API ────────────────────────────────────────────────────────────────

class PlanIn(BaseModel):
    server_id: str = Field(max_length=16)
    name: str = Field(min_length=1, max_length=80)
    kind: str
    db: dict | None = None
    path: str | None = Field(default=None, max_length=4096)
    exclude: list[str] = Field(default_factory=list, max_length=50)
    every_hours: int = Field(default=24, ge=1, le=24 * 30)
    start_hour: int = Field(default=3, ge=0, le=23)
    keep: int = Field(default=7, ge=1, le=365)
    dest_server: str | None = Field(default=None, max_length=16)
    dest_keep: int = Field(default=14, ge=1, le=365)
    delete_local: bool = False
    enabled: bool = True


def _validate(body: PlanIn) -> dict:
    agents.server_row(body.server_id)
    if body.kind == "db":
        d = body.db or {}
        if not all(isinstance(d.get(k), str) and d.get(k) for k in ("engine", "where", "name")):
            raise HTTPException(400, "دیتابیس را انتخاب کنید")
        target = {"db": {k: d[k] for k in ("engine", "where", "name")} | {"size": int(d.get("size") or 0)}}
    elif body.kind == "files":
        if not body.path or not body.path.startswith("/"):
            raise HTTPException(400, "مسیر پوشه باید مطلق باشد")
        target = {"path": body.path, "exclude": [e for e in body.exclude if e.strip()]}
    else:
        raise HTTPException(400, "نوع بک‌آپ نامعتبر")
    if body.dest_server:
        if body.dest_server == body.server_id:
            raise HTTPException(400, "سرور مقصد باید سرور دیگری باشد")
        agents.server_row(body.dest_server)
    if body.delete_local and not body.dest_server:
        raise HTTPException(400, "حذف نسخهٔ محلی فقط وقتی مجاز است که به سرور دیگری کپی شود")
    return target


@router.get("")
async def list_all(user: User = Depends(auth.admin)) -> dict:
    return {"plans": plans(), "runs": runs()}


@router.post("/plans")
async def create(body: PlanIn, request: Request, user: User = Depends(auth.elevated)) -> dict:
    target = _validate(body)
    # A random file prefix per plan: ids can be reused after a delete, and a
    # new plan must never prune an old plan's files.
    prefix = "p" + secrets.token_hex(4)
    pid = db.x("INSERT INTO backup_plans(server_id, name, kind, target, every_hours, start_hour, keep, "
               "dest_server, dest_keep, delete_local, enabled, next_run, created, prefix) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (body.server_id, body.name.strip(), body.kind, json.dumps(target), body.every_hours,
                body.start_hour, body.keep, body.dest_server or None, body.dest_keep, int(body.delete_local),
                int(body.enabled), next_run(body.every_hours, body.start_hour), db.now(), prefix))
    db.audit(user.username, "backup.plan.add", f"{pid} {body.name}", auth.client_ip(request))
    return {"id": pid, "plans": plans()}


@router.put("/plans/{pid}")
async def update(pid: int, body: PlanIn, request: Request, user: User = Depends(auth.elevated)) -> dict:
    if not db.one("SELECT 1 FROM backup_plans WHERE id=?", (pid,)):
        raise HTTPException(404, "برنامه پیدا نشد")
    target = _validate(body)
    db.x("UPDATE backup_plans SET server_id=?, name=?, kind=?, target=?, every_hours=?, start_hour=?, keep=?, "
         "dest_server=?, dest_keep=?, delete_local=?, enabled=?, next_run=? WHERE id=?",
         (body.server_id, body.name.strip(), body.kind, json.dumps(target), body.every_hours, body.start_hour,
          body.keep, body.dest_server or None, body.dest_keep, int(body.delete_local), int(body.enabled),
          next_run(body.every_hours, body.start_hour), pid))
    db.audit(user.username, "backup.plan.edit", str(pid), auth.client_ip(request))
    return {"plans": plans()}


@router.delete("/plans/{pid}")
async def delete(pid: int, request: Request, user: User = Depends(auth.elevated)) -> dict:
    db.x("DELETE FROM backup_plans WHERE id=?", (pid,))
    alerts.forget_subject("backup", f"plan:{pid}")
    db.audit(user.username, "backup.plan.delete", str(pid), auth.client_ip(request))
    return {"plans": plans()}


@router.post("/plans/{pid}/run")
async def run_now(pid: int, request: Request, user: User = Depends(auth.elevated)) -> dict:
    run_id = start(pid, "manual")
    db.audit(user.username, "backup.run", str(pid), auth.client_ip(request))
    return {"run": run_id}
