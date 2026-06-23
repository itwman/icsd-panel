"""Web file manager — safe filesystem operations under allowed roots.

فایل‌منیجر وب: عملیات امن روی فایل‌ها زیر مسیرهای مجاز.
برای کاربر آماتور که SSH/FTP بلد نیست؛ آپلود/ویرایش/حذف/اکسترکت از مرورگر.

امنیت: همهٔ مسیرها به مسیرهای مجاز (webroot و /home) محدود می‌شوند و در برابر
path traversal (مثل ../) با resolve و بررسی پیشوند محافظت می‌شوند.
"""
from __future__ import annotations

import os
import shutil
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app import db

# مسیرهای پایه‌ای که فایل‌منیجر اجازهٔ کار در آن‌ها را دارد.
ALLOWED_ROOTS = [
    Path("/var/www"),
    Path("/home"),
    Path("/srv"),
]

# پسوندهایی که به‌صورت متنی قابل ویرایش‌اند.
TEXT_EXTS = {
    ".txt", ".html", ".htm", ".css", ".js", ".json", ".php", ".py", ".sh",
    ".conf", ".ini", ".env", ".md", ".xml", ".yml", ".yaml", ".sql", ".log",
    ".htaccess", ".ts", ".jsx", ".vue", ".toml", ".cfg",
}
MAX_EDIT_BYTES = 2 * 1024 * 1024      # 2MB cap for in-browser text editing
MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512MB per upload


class FileError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #
def _resolve(path: str) -> Path:
    """Resolve a user-supplied path and ensure it stays within an allowed root."""
    if not path:
        raise FileError("مسیر خالی است / empty path")
    p = Path(path)
    if not p.is_absolute():
        raise FileError("مسیر باید مطلق باشد / path must be absolute")
    # resolve() collapses .. and symlinks; strict=False so non-existent targets work
    try:
        rp = p.resolve(strict=False)
    except (OSError, RuntimeError):
        raise FileError("مسیر نامعتبر / invalid path")
    for root in ALLOWED_ROOTS:
        try:
            root_r = root.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if rp == root_r or root_r in rp.parents:
            return rp
    allowed = ", ".join(str(r) for r in ALLOWED_ROOTS)
    raise FileError(f"خارج از مسیرهای مجاز / outside allowed roots ({allowed})")


def _is_text(p: Path) -> bool:
    return p.suffix.lower() in TEXT_EXTS or p.name in (".htaccess", ".env")


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _entry(p: Path) -> dict:
    try:
        st = p.stat()
        is_dir = p.is_dir()
        return {
            "name": p.name,
            "path": str(p),
            "is_dir": is_dir,
            "size": 0 if is_dir else st.st_size,
            "modified": _fmt(st.st_mtime),
            "mode": oct(st.st_mode & 0o777)[2:],
            "editable": (not is_dir) and _is_text(p) and st.st_size <= MAX_EDIT_BYTES,
        }
    except OSError as e:
        return {"name": p.name, "path": str(p), "is_dir": False, "size": 0,
                "modified": "", "mode": "", "editable": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# Read operations
# --------------------------------------------------------------------------- #
def roots() -> list[dict]:
    """Allowed roots that actually exist on this host (for the UI sidebar)."""
    out = []
    for r in ALLOWED_ROOTS:
        if r.exists():
            out.append({"path": str(r), "name": r.name or str(r)})
    return out


def listdir(path: str) -> dict:
    p = _resolve(path)
    if not p.exists():
        raise FileError(f"مسیر وجود ندارد / not found: {path}")
    if not p.is_dir():
        raise FileError("مسیر یک پوشه نیست / not a directory")
    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            entries.append(_entry(child))
    except PermissionError:
        raise FileError("دسترسی به این پوشه ممکن نیست / permission denied")
    parent = str(p.parent) if any(
        str(p) != str(r.resolve(strict=False)) for r in ALLOWED_ROOTS
    ) and p != p.parent else None
    return {"path": str(p), "parent": parent, "entries": entries}


def read_text(path: str) -> dict:
    p = _resolve(path)
    if not p.is_file():
        raise FileError("فایل یافت نشد / file not found")
    if p.stat().st_size > MAX_EDIT_BYTES:
        raise FileError("فایل برای ویرایش بزرگ است / file too large to edit")
    if not _is_text(p):
        raise FileError("این نوع فایل قابل ویرایش متنی نیست / not a text file")
    content = p.read_text(encoding="utf-8", errors="replace")
    return {"path": str(p), "content": content, "size": p.stat().st_size}


# --------------------------------------------------------------------------- #
# Write operations
# --------------------------------------------------------------------------- #
def write_text(path: str, content: str) -> dict:
    p = _resolve(path)
    if p.exists() and p.is_dir():
        raise FileError("مسیر یک پوشه است / path is a directory")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    db.audit(None, "files.write", str(p))
    return {"path": str(p), "saved": True, "size": p.stat().st_size}


def mkdir(path: str) -> dict:
    p = _resolve(path)
    if p.exists():
        raise FileError("از قبل وجود دارد / already exists")
    p.mkdir(parents=True)
    db.audit(None, "files.mkdir", str(p))
    return {"path": str(p), "created": True}


def new_file(path: str) -> dict:
    p = _resolve(path)
    if p.exists():
        raise FileError("از قبل وجود دارد / already exists")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    db.audit(None, "files.new", str(p))
    return {"path": str(p), "created": True}


def delete(path: str) -> dict:
    p = _resolve(path)
    if not p.exists():
        raise FileError("مسیر وجود ندارد / not found")
    # never allow deleting an allowed root itself
    for r in ALLOWED_ROOTS:
        if p == r.resolve(strict=False):
            raise FileError("حذف مسیر پایه مجاز نیست / cannot delete a base root")
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    db.audit(None, "files.delete", str(p))
    return {"path": str(p), "deleted": True}


def rename(path: str, new_name: str) -> dict:
    p = _resolve(path)
    if not p.exists():
        raise FileError("مسیر وجود ندارد / not found")
    if "/" in new_name or new_name in ("", ".", ".."):
        raise FileError("نام نامعتبر / invalid name")
    target = _resolve(str(p.parent / new_name))
    if target.exists():
        raise FileError("نام مقصد از قبل وجود دارد / target exists")
    p.rename(target)
    db.audit(None, "files.rename", f"{p} -> {target}")
    return {"path": str(target), "renamed": True}


def chmod(path: str, mode: str) -> dict:
    p = _resolve(path)
    if not p.exists():
        raise FileError("مسیر وجود ندارد / not found")
    try:
        m = int(mode, 8)
    except ValueError:
        raise FileError("مود نامعتبر (مثلاً 644) / invalid mode")
    p.chmod(m)
    db.audit(None, "files.chmod", f"{p} {mode}")
    return {"path": str(p), "mode": mode}


def save_upload(dir_path: str, filename: str, data: bytes) -> dict:
    if len(data) > MAX_UPLOAD_BYTES:
        raise FileError("حجم فایل از حد مجاز بیشتر است / file exceeds size limit")
    base = filename.replace("\\", "/").split("/")[-1]  # strip any path component
    if base in ("", ".", ".."):
        raise FileError("نام فایل نامعتبر / invalid filename")
    p = _resolve(str(Path(dir_path) / base))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    db.audit(None, "files.upload", str(p))
    return {"path": str(p), "uploaded": True, "size": len(data)}


def read_bytes(path: str) -> tuple[bytes, str]:
    """For download: return (data, filename). Caller streams it."""
    p = _resolve(path)
    if not p.is_file():
        raise FileError("فایل یافت نشد / file not found")
    return p.read_bytes(), p.name


# --------------------------------------------------------------------------- #
# Archive extract / compress
# --------------------------------------------------------------------------- #
def _safe_within(base: Path, target: Path) -> bool:
    try:
        base_r = base.resolve(strict=False)
        return base_r == target.resolve(strict=False) or base_r in target.resolve(strict=False).parents
    except (OSError, RuntimeError):
        return False


def extract(path: str, dest: str | None = None) -> dict:
    """Extract a .zip / .tar.gz / .tgz / .tar into dest (default: same folder)."""
    p = _resolve(path)
    if not p.is_file():
        raise FileError("فایل آرشیو یافت نشد / archive not found")
    out = _resolve(dest) if dest else p.parent
    out.mkdir(parents=True, exist_ok=True)
    name = p.name.lower()
    count = 0
    if name.endswith(".zip"):
        with zipfile.ZipFile(p) as zf:
            for member in zf.namelist():
                tgt = (out / member)
                if not _safe_within(out, tgt):
                    raise FileError("آرشیو حاوی مسیر ناامن است / unsafe path in archive")
            zf.extractall(out)
            count = len(zf.namelist())
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        mode = "r:gz" if name.endswith((".tar.gz", ".tgz")) else "r:"
        with tarfile.open(p, mode) as tf:
            for member in tf.getmembers():
                tgt = out / member.name
                if not _safe_within(out, tgt):
                    raise FileError("آرشیو حاوی مسیر ناامن است / unsafe path in archive")
            tf.extractall(out)  # noqa: S202 — members validated above
            count = len(tf.getmembers())
    else:
        raise FileError("فرمت آرشیو پشتیبانی نمی‌شود (zip/tar.gz) / unsupported archive")
    db.audit(None, "files.extract", f"{p} -> {out} ({count})")
    return {"path": str(out), "extracted": count}


def compress(paths: list[str], archive_name: str, dest_dir: str) -> dict:
    """Create a .zip from a list of files/folders into dest_dir/archive_name."""
    if not paths:
        raise FileError("موردی برای فشرده‌سازی انتخاب نشده / nothing selected")
    if not archive_name.endswith(".zip"):
        archive_name += ".zip"
    out = _resolve(str(Path(dest_dir) / archive_name))
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for raw in paths:
            src = _resolve(raw)
            if src.is_dir():
                for f in src.rglob("*"):
                    zf.write(f, f.relative_to(src.parent))
            elif src.is_file():
                zf.write(src, src.name)
    db.audit(None, "files.compress", str(out))
    return {"path": str(out), "compressed": len(paths)}
