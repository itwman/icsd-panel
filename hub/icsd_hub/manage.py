"""Phases 2–4 on one server: files, services, containers, console,
databases, certificates. The hub relays; the agent enforces its policy.

Reading needs an admin. Anything that changes a server needs an admin who
re-confirmed with a 2FA code in the last 15 minutes. Every change is audited.

مدیریت هر سرور: خواندن برای مدیر؛ هر تغییر نیازمند تأیید دوباره با رمز دوم و ثبت در گزارش.
"""
from __future__ import annotations

import base64
import posixpath
import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from icsd_hub import agents, auth, db, live, rpc
from icsd_hub.auth import User

router = APIRouter(prefix="/api/servers/{sid}")
CHUNK = 512 * 1024
EDIT_MAX = 2 * 1024 * 1024


async def _call(sid: str, op: str, args: dict | None = None, timeout: float = 60) -> Any:
    agents.server_row(sid)  # 404 for an unknown server
    try:
        return await rpc.call(sid, op, args, timeout)
    except rpc.RPCError as e:
        raise HTTPException(e.status, str(e)) from None


def _audit(user: User, request: Request, sid: str, action: str, detail: str = "") -> None:
    db.audit(user.username, action, f"{sid} {detail}"[:500], auth.client_ip(request))


@router.get("/caps")
async def caps(sid: str, user: User = Depends(auth.admin)) -> dict:
    agents.server_row(sid)
    lv = live.peek(sid)
    return {"caps": (lv.caps if lv else {}) or {}, "online": bool(lv and lv.ws)}


# ── files ──────────────────────────────────────────────────────────────

class PathIn(BaseModel):
    path: str = Field(max_length=4096)


class PathsIn(BaseModel):
    paths: list[str] = Field(max_length=500)


class WriteIn(BaseModel):
    path: str = Field(max_length=4096)
    text: str
    if_mtime: int = 0


class RenameIn(BaseModel):
    src: str = Field(max_length=4096, alias="from")
    to: str = Field(max_length=4096)


class ChmodIn(BaseModel):
    path: str = Field(max_length=4096)
    mode: str = Field(max_length=4)


class ArchiveIn(BaseModel):
    paths: list[str] = Field(max_length=500)
    dest: str = Field(max_length=4096)


class ExtractIn(BaseModel):
    path: str = Field(max_length=4096)
    dest: str = Field(max_length=4096)


@router.get("/fs/list")
async def fs_list(sid: str, path: str = "", user: User = Depends(auth.admin)) -> Any:
    return await _call(sid, "fs.list", {"path": path})


@router.get("/fs/read")
async def fs_read(sid: str, path: str, user: User = Depends(auth.admin)) -> dict:
    """Whole small file as text, for the editor."""
    first = await _call(sid, "fs.read", {"path": path, "offset": 0, "length": CHUNK})
    size = first["size"]
    if size > EDIT_MAX:
        raise HTTPException(413, "برای ویرایش در مرورگر بزرگ است (بیش از ۲ مگابایت)؛ دانلود کنید")
    data = base64.b64decode(first["data"])
    while len(data) < size:
        nxt = await _call(sid, "fs.read", {"path": path, "offset": len(data), "length": CHUNK})
        chunk = base64.b64decode(nxt["data"])
        if not chunk:
            break
        data += chunk
    if b"\x00" in data[:8192]:
        raise HTTPException(415, "فایل متنی نیست؛ دانلود کنید")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return {"path": path, "text": text, "mtime": first["mtime"], "size": size}


@router.get("/fs/download")
async def fs_download(sid: str, path: str, request: Request, user: User = Depends(auth.admin)) -> StreamingResponse:
    first = await _call(sid, "fs.read", {"path": path, "offset": 0, "length": CHUNK})
    _audit(user, request, sid, "fs.download", path)
    size = first["size"]

    async def stream():
        data = base64.b64decode(first["data"])
        offset = len(data)
        yield data
        while offset < size:
            nxt = await rpc.call(sid, "fs.read", {"path": path, "offset": offset, "length": CHUNK})
            chunk = base64.b64decode(nxt["data"])
            if not chunk:
                break
            offset += len(chunk)
            yield chunk

    name = posixpath.basename(path) or "download"
    return StreamingResponse(stream(), media_type="application/octet-stream", headers={
        "Content-Length": str(size),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
    })


async def _write_bytes(sid: str, path: str, data: bytes, if_mtime: int = 0) -> Any:
    offset = 0
    upload = ""
    while True:
        chunk = data[offset:offset + CHUNK]
        final = offset + len(chunk) >= len(data)
        args = {"path": path, "data": base64.b64encode(chunk).decode(), "offset": offset, "final": final,
                "upload": upload}
        if offset == 0 and if_mtime:
            args["if_mtime"] = if_mtime
        res = await _call(sid, "fs.write", args)
        upload = (res or {}).get("upload", upload)
        offset += len(chunk)
        if final:
            return res


@router.post("/fs/write")
async def fs_write(sid: str, body: WriteIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    data = body.text.encode("utf-8")
    if len(data) > EDIT_MAX:
        raise HTTPException(413, "بزرگ‌تر از ۲ مگابایت")
    res = await _write_bytes(sid, body.path, data, body.if_mtime)
    _audit(user, request, sid, "fs.write", body.path)
    return res


@router.post("/fs/upload")
async def fs_upload(sid: str, request: Request, dir: str, user: User = Depends(auth.elevated)) -> dict:
    form = await request.form()
    files = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
    if not files:
        raise HTTPException(400, "فایلی نرسید")
    done = []
    for f in files:
        name = posixpath.basename((f.filename or "").replace("\\", "/"))
        if not name or name in (".", ".."):
            continue
        target = posixpath.join(dir, name)
        offset = 0
        upload = ""
        chunk = await f.read(CHUNK)
        while True:
            nxt = await f.read(CHUNK)  # read ahead: the agent must know which chunk is last
            final = not nxt
            res = await _call(sid, "fs.write", {"path": target, "data": base64.b64encode(chunk).decode(),
                                                "offset": offset, "final": final, "upload": upload})
            upload = (res or {}).get("upload", upload)
            offset += len(chunk)
            if final:
                break
            chunk = nxt
        done.append({"name": name, "size": offset})
        _audit(user, request, sid, "fs.upload", f"{target} ({offset} B)")
    return {"uploaded": done}


@router.post("/fs/mkdir")
async def fs_mkdir(sid: str, body: PathIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    res = await _call(sid, "fs.mkdir", {"path": body.path})
    _audit(user, request, sid, "fs.mkdir", body.path)
    return res


@router.post("/fs/rename")
async def fs_rename(sid: str, body: RenameIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    res = await _call(sid, "fs.rename", {"from": body.src, "to": body.to})
    _audit(user, request, sid, "fs.rename", f"{body.src} → {body.to}")
    return res


@router.post("/fs/delete")
async def fs_delete(sid: str, body: PathsIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    _audit(user, request, sid, "fs.delete", ", ".join(body.paths)[:450])
    return await _call(sid, "fs.delete", {"paths": body.paths}, timeout=120)


@router.post("/fs/chmod")
async def fs_chmod(sid: str, body: ChmodIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    res = await _call(sid, "fs.chmod", {"path": body.path, "mode": body.mode})
    _audit(user, request, sid, "fs.chmod", f"{body.path} {body.mode}")
    return res


@router.post("/fs/archive")
async def fs_archive(sid: str, body: ArchiveIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    _audit(user, request, sid, "fs.archive", body.dest)
    return await _call(sid, "fs.archive", {"paths": body.paths, "dest": body.dest})


@router.post("/fs/extract")
async def fs_extract(sid: str, body: ExtractIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    _audit(user, request, sid, "fs.extract", f"{body.path} → {body.dest}")
    return await _call(sid, "fs.extract", {"path": body.path, "dest": body.dest})


@router.get("/fs/tail")
async def fs_tail(sid: str, path: str, lines: int = 300, grep: str = "",
                  user: User = Depends(auth.admin)) -> Any:
    return await _call(sid, "fs.tail", {"path": path, "lines": lines, "grep": grep[:200]})


# ── services, containers, console ──────────────────────────────────────

class SvcIn(BaseModel):
    name: str = Field(max_length=128)
    action: str = Field(max_length=16)


class DockerIn(BaseModel):
    id: str = Field(max_length=128)
    action: str = Field(max_length=16)


class ConsoleIn(BaseModel):
    cmd: str = Field(max_length=4000)
    cwd: str = Field(default="", max_length=1024)
    timeout: int = Field(default=60, ge=1, le=300)


@router.get("/services")
async def services(sid: str, user: User = Depends(auth.admin)) -> Any:
    return await _call(sid, "svc.list")


@router.post("/services/action")
async def service_action(sid: str, body: SvcIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    _audit(user, request, sid, f"svc.{body.action}", body.name)
    return await _call(sid, "svc.action", body.model_dump(), timeout=120)


@router.get("/services/logs")
async def service_logs(sid: str, name: str, lines: int = 300, user: User = Depends(auth.admin)) -> Any:
    return await _call(sid, "svc.logs", {"name": name, "lines": lines})


@router.post("/docker/action")
async def docker_action(sid: str, body: DockerIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    _audit(user, request, sid, f"docker.{body.action}", body.id)
    return await _call(sid, "docker.action", body.model_dump(), timeout=120)


@router.get("/docker/logs")
async def docker_logs(sid: str, id: str, lines: int = 300, user: User = Depends(auth.admin)) -> Any:
    return await _call(sid, "docker.logs", {"id": id, "lines": lines})


@router.post("/console")
async def console(sid: str, body: ConsoleIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    _audit(user, request, sid, "console", body.cmd)
    return await _call(sid, "exec.run", body.model_dump(), timeout=body.timeout + 15)


# ── databases ──────────────────────────────────────────────────────────

class DBRef(BaseModel):
    engine: str = Field(max_length=16)
    where: str = Field(max_length=140)
    name: str = Field(max_length=64)
    size: int = 0


class RestoreIn(BaseModel):
    db: DBRef
    file: str = Field(max_length=4096)
    confirm: str = Field(max_length=64)


@router.get("/databases")
async def databases(sid: str, user: User = Depends(auth.admin)) -> Any:
    return await _call(sid, "db.list", timeout=90)


@router.post("/databases/dump")
async def db_dump(sid: str, body: DBRef, request: Request, user: User = Depends(auth.elevated)) -> Any:
    _audit(user, request, sid, "db.dump", f"{body.where}/{body.name}")
    return await _call(sid, "db.dump", body.model_dump())


@router.post("/databases/restore")
async def db_restore(sid: str, body: RestoreIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    if body.confirm != body.db.name:
        raise HTTPException(400, "برای تأیید، نام دیتابیس را دقیق بنویسید")
    _audit(user, request, sid, "db.restore", f"{body.db.where}/{body.db.name} ← {body.file}")
    return await _call(sid, "db.restore", {**body.db.model_dump(), "file": body.file})


# ── certificates ───────────────────────────────────────────────────────

class RenewIn(BaseModel):
    name: str = Field(max_length=128)
    force: bool = False


class IssueIn(BaseModel):
    domains: list[str] = Field(max_length=20)
    email: str = Field(default="", max_length=200)
    redirect: bool = True


@router.get("/ssl")
async def ssl_list(sid: str, user: User = Depends(auth.admin)) -> Any:
    return await _call(sid, "ssl.list", timeout=30)


@router.post("/ssl/renew")
async def ssl_renew(sid: str, body: RenewIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    _audit(user, request, sid, "ssl.renew", body.name)
    return await _call(sid, "ssl.renew", body.model_dump())


@router.post("/ssl/issue")
async def ssl_issue(sid: str, body: IssueIn, request: Request, user: User = Depends(auth.elevated)) -> Any:
    _audit(user, request, sid, "ssl.issue", " ".join(body.domains))
    return await _call(sid, "ssl.issue", body.model_dump())


# ── background jobs and backup files ───────────────────────────────────

_JOB_ID = re.compile(r"^[0-9a-f]{16}$")


@router.get("/jobs/{job_id}")
async def job(sid: str, job_id: str, user: User = Depends(auth.admin)) -> Any:
    if not _JOB_ID.match(job_id):
        raise HTTPException(404)
    return await _call(sid, "job.status", {"id": job_id})


@router.get("/jobs")
async def jobs(sid: str, user: User = Depends(auth.admin)) -> Any:
    return await _call(sid, "job.list")


@router.get("/backup-files")
async def backup_files(sid: str, user: User = Depends(auth.admin)) -> Any:
    return await _call(sid, "backup.list")
