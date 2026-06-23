"""File manager endpoints — browse, edit, upload, download, archive.

نقاط پایانی فایل‌منیجر: مرور، ویرایش، آپلود، دانلود، فشرده‌سازی/اکسترکت.
نقش readonly فقط می‌تواند مرور/دانلود کند؛ تغییرات نیازمند نقش manager است.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import filemanager as fm
from app.services.filemanager import FileError
from app.core.deps import get_current_user, require_role

router = APIRouter(prefix="/api/files", tags=["files"])


class PathIn(BaseModel):
    path: str


class WriteIn(BaseModel):
    path: str
    content: str


class RenameIn(BaseModel):
    path: str
    new_name: str


class ChmodIn(BaseModel):
    path: str
    mode: str


class ExtractIn(BaseModel):
    path: str
    dest: str | None = None


class CompressIn(BaseModel):
    paths: list[str]
    archive_name: str
    dest_dir: str


class TransferIn(BaseModel):
    paths: list[str]
    dest_dir: str


class PathsIn(BaseModel):
    paths: list[str]


def _guard(fn, *a, **k):
    try:
        return fn(*a, **k)
    except FileError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/roots")
async def get_roots(user: dict = Depends(get_current_user)) -> dict:
    return {"roots": fm.roots()}


@router.get("/list")
async def list_dir(path: str, user: dict = Depends(get_current_user)) -> dict:
    return _guard(fm.listdir, path)


@router.get("/read")
async def read_file(path: str, user: dict = Depends(get_current_user)) -> dict:
    return _guard(fm.read_text, path)


@router.get("/download")
async def download(path: str, user: dict = Depends(get_current_user)):
    data, name = _guard(fm.read_bytes, path)
    return StreamingResponse(
        iter([data]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/write")
async def write_file(body: WriteIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.write_text, body.path, body.content)


@router.post("/mkdir")
async def make_dir(body: PathIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.mkdir, body.path)


@router.post("/new")
async def new_file(body: PathIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.new_file, body.path)


@router.post("/delete")
async def delete_path(body: PathIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.delete, body.path)


@router.post("/rename")
async def rename_path(body: RenameIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.rename, body.path, body.new_name)


@router.post("/chmod")
async def chmod_path(body: ChmodIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.chmod, body.path, body.mode)


@router.post("/extract")
async def extract_archive(body: ExtractIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.extract, body.path, body.dest)


@router.post("/compress")
async def compress_paths(body: CompressIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.compress, body.paths, body.archive_name, body.dest_dir)


@router.post("/copy")
async def copy_paths(body: TransferIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.copy_items, body.paths, body.dest_dir)


@router.post("/move")
async def move_paths(body: TransferIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.move_items, body.paths, body.dest_dir)


@router.post("/delete-many")
async def delete_many(body: PathsIn, user: dict = Depends(require_role("manager"))) -> dict:
    return _guard(fm.delete_items, body.paths)


@router.post("/upload")
async def upload(dir_path: str = Form(...), file: UploadFile = File(...),
                 user: dict = Depends(require_role("manager"))) -> dict:
    data = await file.read()
    return _guard(fm.save_upload, dir_path, file.filename or "upload.bin", data)
