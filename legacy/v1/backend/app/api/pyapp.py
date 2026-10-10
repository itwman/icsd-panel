"""Python/Django app deployment endpoints.

نقاط پایانی استقرار اپ پایتون/جنگو: فهرست، ساخت (از گیت)، redeploy، کنترل
سرویس، لاگ، و حذف.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.services import pyapp as pyapp_service
from app.services.pyapp import PyAppError
from app.services.sites import SiteError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/pyapps", tags=["pyapps"])


class PyAppIn(BaseModel):
    name: str
    domain: str = ""
    repo_url: str
    branch: str = "main"
    app_type: str = "django"          # django | wsgi | asgi
    entry: str = ""
    env_vars: str = ""
    python_version: str | None = None
    dry: bool = False


class TemplateIn(BaseModel):
    name: str
    domain: str = ""
    template: str                     # fastapi | flask | django
    env_vars: str = ""
    python_version: str | None = None
    dry: bool = False


class ControlIn(BaseModel):
    action: str                       # start | stop | restart | status


@router.get("")
async def list_apps(user: dict = Depends(get_current_user)) -> dict:
    return {"apps": pyapp_service.list_apps()}


@router.get("/templates")
async def list_templates(user: dict = Depends(get_current_user)) -> dict:
    return {"templates": pyapp_service.list_templates()}


@router.post("/template", status_code=201)
async def create_from_template(body: TemplateIn,
                               user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return pyapp_service.create_from_template(
            body.name, body.domain, body.template, env_vars=body.env_vars,
            python_version=body.python_version, apply=not body.dry)
    except (PyAppError, SiteError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{app_id}/webhook-info")
async def webhook_info(app_id: int, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return pyapp_service.get_webhook_info(app_id)
    except PyAppError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{app_id}/webhook-regenerate")
async def webhook_regenerate(app_id: int,
                             user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return pyapp_service.regenerate_secret(app_id)
    except PyAppError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{app_id}/webhook")
async def github_webhook(app_id: int, request: Request) -> dict:
    """Unauthenticated endpoint called by GitHub; verified via HMAC signature."""
    body = await request.body()
    sig = request.headers.get("x-hub-signature-256")
    event = request.headers.get("x-github-event")
    ref = None
    try:
        import json
        ref = (json.loads(body or b"{}") or {}).get("ref")
    except Exception:  # noqa
        ref = None
    try:
        return pyapp_service.handle_webhook(app_id, body, sig, event, ref)
    except PyAppError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post("", status_code=201)
async def create_app(body: PyAppIn, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return pyapp_service.create_app(
            body.name, body.domain, body.repo_url, branch=body.branch,
            app_type=body.app_type, entry=body.entry, env_vars=body.env_vars,
            python_version=body.python_version, apply=not body.dry)
    except (PyAppError, SiteError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{app_id}/deploy")
async def deploy(app_id: int, user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return pyapp_service.deploy(app_id)
    except PyAppError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{app_id}/control")
async def control(app_id: int, body: ControlIn,
                  user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return pyapp_service.control(app_id, body.action)
    except PyAppError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{app_id}/logs")
async def app_logs(app_id: int, lines: int = 200,
                   user: dict = Depends(get_current_user)) -> dict:
    try:
        return pyapp_service.logs(app_id, lines)
    except PyAppError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{app_id}")
async def delete_app(app_id: int, remove_files: bool = False,
                     user: dict = Depends(require_role("manager"))) -> dict:
    try:
        return pyapp_service.delete_app(app_id, remove_files=remove_files)
    except PyAppError as e:
        raise HTTPException(status_code=400, detail=str(e))
