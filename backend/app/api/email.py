"""Email management endpoints.

نقاط پایانی مدیریت ایمیل.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services import email
from app.services.email import MailError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/email", tags=["email"])


class DomainIn(BaseModel):
    domain: str


class MailboxIn(BaseModel):
    address: str
    password: str = Field(..., min_length=8)
    quota_mb: int = 1024


class PasswordIn(BaseModel):
    address: str
    new_password: str = Field(..., min_length=8)


class AliasIn(BaseModel):
    source: str
    destination: str


@router.get("/domains")
async def domains(user: dict = Depends(get_current_user)) -> dict:
    return {"domains": email.list_domains()}


@router.post("/domains", status_code=201)
async def add_domain(body: DomainIn, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return email.add_domain(body.domain)
    except MailError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/domains/{domain}")
async def del_domain(domain: str, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return email.delete_domain(domain)
    except MailError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/mailboxes")
async def mailboxes(domain: str | None = None, user: dict = Depends(get_current_user)) -> dict:
    return {"mailboxes": email.list_mailboxes(domain)}


@router.post("/mailboxes", status_code=201)
async def create_mailbox(body: MailboxIn, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return email.create_mailbox(body.address, body.password, body.quota_mb)
    except MailError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/mailboxes/password")
async def change_password(body: PasswordIn, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return email.change_mailbox_password(body.address, body.new_password)
    except MailError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/mailboxes/{address}")
async def delete_mailbox(address: str, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return email.delete_mailbox(address)
    except MailError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/aliases")
async def aliases(domain: str | None = None, user: dict = Depends(get_current_user)) -> dict:
    return {"aliases": email.list_aliases(domain)}


@router.post("/aliases", status_code=201)
async def create_alias(body: AliasIn, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return email.create_alias(body.source, body.destination)
    except MailError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/aliases/{alias_id}")
async def delete_alias(alias_id: int, user: dict = Depends(require_role("manager"))) -> dict:
    return email.delete_alias(alias_id)


@router.get("/postfix-maps")
async def postfix_maps(user: dict = Depends(require_role("admin"))) -> dict:
    return email.generate_postfix_maps()
