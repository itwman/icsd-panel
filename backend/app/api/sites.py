"""Site / domain management endpoints.

نقاط پایانی مدیریت سایت و دامنه.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import sites
from app.services.sites import SiteError

router = APIRouter(prefix="/api/sites", tags=["sites"])


class SiteCreate(BaseModel):
    domain: str = Field(..., examples=["example.ir"])
    site_type: str = Field("static", description="static | php | proxy")
    aliases: str = Field("", description="comma-separated extra domains, e.g. www.example.ir")
    php_version: str | None = Field(None, examples=["8.2"])
    proxy_pass: str | None = Field(None, examples=["http://127.0.0.1:3000"])
    webroot: str | None = None
    apply: bool = True


class PreviewRequest(SiteCreate):
    pass


@router.get("")
async def list_sites() -> dict:
    return {"sites": sites.list_sites()}


@router.post("", status_code=201)
async def create_site(body: SiteCreate) -> dict:
    try:
        return sites.create_site(
            domain=body.domain, site_type=body.site_type, aliases=body.aliases,
            php_version=body.php_version, proxy_pass=body.proxy_pass,
            webroot=body.webroot, apply=body.apply,
        )
    except SiteError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/preview")
async def preview_vhost(body: PreviewRequest) -> dict:
    """Render the vhost config without writing anything — for the UI preview pane."""
    try:
        domain = sites.validate_domain(body.domain)
        alias_list = [sites.validate_domain(a) for a in body.aliases.split(",") if a.strip()]
        server_names = " ".join([domain] + alias_list)
        webroot = body.webroot or f"{sites.DEFAULT_WEBROOT_BASE}/{domain}"
        config = sites.render_vhost(
            domain=domain, site_type=body.site_type, webroot=webroot,
            server_names=server_names, php_version=body.php_version,
            proxy_pass=body.proxy_pass, ssl_enabled=False,
        )
        return {"domain": domain, "config": config}
    except SiteError as e:
        raise HTTPException(status_code=400, detail=str(e))


class SubdomainCreate(BaseModel):
    parent_domain: str
    label: str
    site_type: str = "static"
    php_version: str | None = None
    proxy_pass: str | None = None
    apply: bool = True


@router.post("/subdomain", status_code=201)
async def create_subdomain(body: SubdomainCreate) -> dict:
    try:
        return sites.create_subdomain(
            parent_domain=body.parent_domain, label=body.label, site_type=body.site_type,
            php_version=body.php_version, proxy_pass=body.proxy_pass, apply=body.apply,
        )
    except SiteError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/subdomains/{parent}")
async def list_subdomains(parent: str) -> dict:
    try:
        return {"parent": parent, "subdomains": sites.list_subdomains(parent)}
    except SiteError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{domain}")
async def get_site(domain: str) -> dict:
    site = sites.get_site(domain)
    if not site:
        raise HTTPException(status_code=404, detail="site not found")
    return site


@router.post("/{domain}/enable")
async def enable_site(domain: str) -> dict:
    try:
        return sites.set_enabled(domain, True)
    except SiteError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{domain}/disable")
async def disable_site(domain: str) -> dict:
    try:
        return sites.set_enabled(domain, False)
    except SiteError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{domain}")
async def delete_site(domain: str, remove_webroot: bool = False) -> dict:
    try:
        return sites.delete_site(domain, remove_webroot=remove_webroot)
    except SiteError as e:
        raise HTTPException(status_code=400, detail=str(e))
