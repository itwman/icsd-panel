"""DNS / zone editor endpoints.

نقاط پایانی DNS: مدیریت zone و رکوردها، export فایل zone، و اعمال روی BIND.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.services import dns as dns_service
from app.services.dns import DNSError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/dns", tags=["dns"])


class ZoneIn(BaseModel):
    domain: str
    ns1: str = ""
    ns2: str = ""
    admin_email: str = ""
    ttl: int = 3600


class RecordIn(BaseModel):
    type: str
    name: str = "@"
    content: str
    ttl: int = 3600
    priority: int | None = None


@router.get("")
async def list_zones(user: dict = Depends(get_current_user)) -> dict:
    return {"zones": dns_service.list_zones(), "status": dns_service.status()}


@router.post("", status_code=201)
async def create_zone(body: ZoneIn, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return dns_service.create_zone(body.domain, body.ns1, body.ns2,
                                       body.admin_email, body.ttl)
    except DNSError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{zone_id}/records")
async def list_records(zone_id: int, user: dict = Depends(get_current_user)) -> dict:
    zone = dns_service.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="zone not found")
    return {"zone": zone, "records": dns_service.list_records(zone_id),
            "types": sorted(dns_service.RECORD_TYPES)}


@router.get("/{zone_id}/export", response_class=PlainTextResponse)
async def export_zone(zone_id: int, user: dict = Depends(get_current_user)) -> str:
    try:
        return dns_service.export_zonefile(zone_id)
    except DNSError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{zone_id}/apply")
async def apply_zone(zone_id: int, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return dns_service.apply_zone(zone_id)
    except DNSError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{zone_id}")
async def delete_zone(zone_id: int, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return dns_service.delete_zone(zone_id)
    except DNSError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{zone_id}/records", status_code=201)
async def add_record(zone_id: int, body: RecordIn,
                     user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return dns_service.add_record(zone_id, body.type, body.name, body.content,
                                      body.ttl, body.priority)
    except DNSError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/records/{record_id}")
async def update_record(record_id: int, body: RecordIn,
                        user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return dns_service.update_record(record_id, body.type, body.name, body.content,
                                         body.ttl, body.priority)
    except DNSError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/records/{record_id}")
async def delete_record(record_id: int, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return dns_service.delete_record(record_id)
    except DNSError as e:
        raise HTTPException(status_code=400, detail=str(e))
