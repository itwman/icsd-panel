"""Database management endpoints — MySQL/MariaDB + PostgreSQL.

نقاط پایانی مدیریت دیتابیس — MySQL/MariaDB و PostgreSQL (انتزاع موتور).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services import databases as mysql_svc
from app.services import postgres as pg_svc
from app.services.databases import DBError
from app.services.postgres import PGError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/databases", tags=["databases"])


def _svc(engine: str):
    engine = (engine or "mysql").lower()
    if engine in ("postgres", "postgresql", "pg"):
        return pg_svc, PGError
    return mysql_svc, DBError


class DBCreate(BaseModel):
    name: str
    user: str | None = None
    password: str | None = None
    engine: str = "mysql"          # mysql | postgres
    apply: bool = True


@router.get("")
async def list_dbs(user: dict = Depends(get_current_user)) -> dict:
    out = {"engines": {}}
    out["engines"]["mysql"] = mysql_svc.list_databases()
    out["engines"]["postgres"] = pg_svc.list_databases()
    # flat managed list for simple UIs
    out["managed"] = out["engines"]["mysql"]["managed"] + out["engines"]["postgres"]["managed"]
    return out


@router.get("/engines")
async def engines(user: dict = Depends(get_current_user)) -> dict:
    return {"mysql": mysql_svc.oscmd.has("mysql"), "postgres": pg_svc.available()}


@router.post("", status_code=201)
async def create_db(body: DBCreate, user: dict = Depends(require_role("manager"))) -> dict:
    svc, Err = _svc(body.engine)
    try:
        return svc.create_database(body.name, body.user, body.password, body.apply)
    except Err as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{name}/reset-password")
async def reset_pw(name: str, engine: str = "mysql",
                   user: dict = Depends(require_role("manager"))) -> dict:
    svc, Err = _svc(engine)
    try:
        return svc.reset_password(name)
    except Err as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{name}")
async def delete_db(name: str, engine: str = "mysql", drop_user: bool = True,
                    user: dict = Depends(require_role("manager"))) -> dict:
    svc, Err = _svc(engine)
    try:
        return svc.delete_database(name, drop_user=drop_user)
    except Err as e:
        raise HTTPException(status_code=400, detail=str(e))
