"""Backup system — archive sites/databases and ship to a remote host.

سیستم بک‌آپ: آرشیو سایت‌ها/دیتابیس‌ها و انتقال به هاست دیگر از طریق FTP/SFTP.
FTP از ftplib استاندارد و SFTP از paramiko استفاده می‌کند. چرخش نگه‌داری (retention)
فایل‌های قدیمی را روی مقصد حذف می‌کند. بازیابی (restore) آرشیو را از مقصد دانلود و
به سایت/مسیر/دیتابیس برمی‌گرداند.
"""
from __future__ import annotations

import os
import tarfile
import tempfile
import ftplib
from datetime import datetime, timezone

from app.core import oscmd
from app.services.sites import get_site
from app import db


class BackupError(Exception):
    pass


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------- #
# Archive creation
# --------------------------------------------------------------------------- #
def archive_path(source_type: str, source_ref: str, work_dir: str) -> str:
    """Create a local .tar.gz / .sql.gz for the source. Returns the file path."""
    ts = _timestamp()
    if source_type == "site":
        site = get_site(source_ref)
        if not site:
            raise BackupError(f"سایت یافت نشد / site not found: {source_ref}")
        root = site["webroot"]
        if not os.path.isdir(root):
            raise BackupError(f"webroot وجود ندارد / webroot missing: {root}")
        out = os.path.join(work_dir, f"site-{source_ref}-{ts}.tar.gz")
        with tarfile.open(out, "w:gz") as tar:
            tar.add(root, arcname=source_ref)
        return out

    if source_type == "path":
        if not os.path.exists(source_ref):
            raise BackupError(f"مسیر وجود ندارد / path missing: {source_ref}")
        safe = source_ref.strip("/").replace("/", "_") or "root"
        out = os.path.join(work_dir, f"path-{safe}-{ts}.tar.gz")
        with tarfile.open(out, "w:gz") as tar:
            tar.add(source_ref, arcname=os.path.basename(source_ref.rstrip("/")) or "root")
        return out

    if source_type == "database":
        if not oscmd.has("mysqldump"):
            raise BackupError("mysqldump نصب نیست / mysqldump not installed")
        out = os.path.join(work_dir, f"db-{source_ref}-{ts}.sql.gz")
        # mysqldump | gzip — shell pipe handled via two-step to avoid shell=True
        dump = oscmd.run(["mysqldump", "--single-transaction", source_ref], timeout=600)
        if not dump.ok:
            raise BackupError(f"mysqldump failed: {dump.stderr}")
        import gzip
        with gzip.open(out, "wt", encoding="utf-8") as f:
            f.write(dump.stdout)
        return out

    raise BackupError(f"نوع منبع نامعتبر / invalid source_type: {source_type}")


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
def _upload_ftp(local: str, job: dict) -> None:
    with ftplib.FTP() as ftp:
        ftp.connect(job["dest_host"], int(job.get("dest_port") or 21), timeout=60)
        ftp.login(job["dest_user"], job["dest_password"])
        _ftp_chdir(ftp, job.get("dest_path") or "/")
        with open(local, "rb") as fh:
            ftp.storbinary(f"STOR {os.path.basename(local)}", fh)
        _ftp_retention(ftp, job)


def _ftp_chdir(ftp: ftplib.FTP, path: str) -> None:
    for part in path.strip("/").split("/"):
        if not part:
            continue
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            ftp.mkd(part)
            ftp.cwd(part)


def _ftp_retention(ftp: ftplib.FTP, job: dict) -> None:
    keep = int(job.get("retention") or 7)
    prefix = _prefix_for(job)
    try:
        names = sorted(n for n in ftp.nlst() if n.startswith(prefix))
    except ftplib.error_perm:
        return
    for old in names[:-keep] if len(names) > keep else []:
        try:
            ftp.delete(old)
        except ftplib.error_perm:
            pass


def _upload_sftp(local: str, job: dict) -> None:
    try:
        import paramiko
    except ImportError:
        raise BackupError("paramiko نصب نیست (برای SFTP) / paramiko not installed")
    transport = paramiko.Transport((job["dest_host"], int(job.get("dest_port") or 22)))
    transport.connect(username=job["dest_user"], password=job["dest_password"])
    try:
        sftp = paramiko.SFTPClient.from_transport(transport)
        remote_dir = job.get("dest_path") or "/"
        _sftp_makedirs(sftp, remote_dir)
        remote = remote_dir.rstrip("/") + "/" + os.path.basename(local)
        sftp.put(local, remote)
        _sftp_retention(sftp, remote_dir, job)
    finally:
        transport.close()


def _sftp_makedirs(sftp, path: str) -> None:
    cur = ""
    for part in path.strip("/").split("/"):
        if not part:
            continue
        cur += "/" + part
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def _sftp_retention(sftp, remote_dir: str, job: dict) -> None:
    keep = int(job.get("retention") or 7)
    prefix = _prefix_for(job)
    try:
        names = sorted(n for n in sftp.listdir(remote_dir) if n.startswith(prefix))
    except IOError:
        return
    for old in names[:-keep] if len(names) > keep else []:
        try:
            sftp.remove(remote_dir.rstrip("/") + "/" + old)
        except IOError:
            pass


def _prefix_for(job: dict) -> str:
    kind = {"site": "site", "database": "db", "path": "path"}.get(job["source_type"], "bk")
    ref = job["source_ref"].strip("/").replace("/", "_") or "root"
    return f"{kind}-{ref}-"


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def run_job(job: dict, apply: bool = True) -> dict:
    """Create archive and upload. Returns status dict and updates last_run/last_status."""
    status = {"job": job.get("name"), "source": f"{job['source_type']}:{job['source_ref']}"}
    if not apply:
        with tempfile.TemporaryDirectory() as tmp:
            path = archive_path(job["source_type"], job["source_ref"], tmp)
            status.update(archive=os.path.basename(path),
                          size_bytes=os.path.getsize(path), uploaded=False, applied=False)
        return status

    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = archive_path(job["source_type"], job["source_ref"], tmp)
            size = os.path.getsize(path)
            dest = job.get("dest_type", "ftp")
            if dest == "ftp":
                _upload_ftp(path, job)
            elif dest == "sftp":
                _upload_sftp(path, job)
            elif dest == "local":
                import shutil
                os.makedirs(job["dest_path"], exist_ok=True)
                shutil.copy2(path, job["dest_path"])
            else:
                raise BackupError(f"مقصد نامعتبر / invalid dest_type: {dest}")
            status.update(archive=os.path.basename(path), size_bytes=size, uploaded=True)
        _update_job_status(job.get("id"), "success")
        db.audit(None, "backup.run", f"{status['source']} -> {job.get('dest_type')}")
        return status
    except Exception as e:
        _update_job_status(job.get("id"), f"failed: {e}")
        raise


def _update_job_status(job_id, status: str) -> None:
    if not job_id:
        return
    with db.get_conn() as conn:
        conn.execute("UPDATE backup_jobs SET last_run=datetime('now'), last_status=? WHERE id=?",
                     (status[:200], job_id))


# --------------------------------------------------------------------------- #
# Restore — list archives on the destination, download, and restore
# --------------------------------------------------------------------------- #
def list_archives(job: dict) -> list[dict]:
    """List available archive files on the job's destination for this source."""
    prefix = _prefix_for(job)
    dest = job.get("dest_type", "ftp")
    items: list[dict] = []
    if dest == "ftp":
        with ftplib.FTP() as ftp:
            ftp.connect(job["dest_host"], int(job.get("dest_port") or 21), timeout=60)
            ftp.login(job["dest_user"], job["dest_password"])
            _ftp_chdir(ftp, job.get("dest_path") or "/")
            try:
                names = [n for n in ftp.nlst() if n.startswith(prefix)]
            except ftplib.error_perm:
                names = []
            for n in names:
                size = None
                try:
                    size = ftp.size(n)
                except Exception:  # noqa
                    pass
                items.append({"name": n, "size": size})
    elif dest == "sftp":
        try:
            import paramiko
        except ImportError:
            raise BackupError("paramiko نصب نیست / paramiko not installed")
        transport = paramiko.Transport((job["dest_host"], int(job.get("dest_port") or 22)))
        transport.connect(username=job["dest_user"], password=job["dest_password"])
        try:
            sftp = paramiko.SFTPClient.from_transport(transport)
            remote_dir = job.get("dest_path") or "/"
            try:
                for attr in sftp.listdir_attr(remote_dir):
                    if attr.filename.startswith(prefix):
                        items.append({"name": attr.filename, "size": attr.st_size})
            except IOError:
                pass
        finally:
            transport.close()
    elif dest == "local":
        if os.path.isdir(job["dest_path"]):
            for n in os.listdir(job["dest_path"]):
                if n.startswith(prefix):
                    fp = os.path.join(job["dest_path"], n)
                    items.append({"name": n, "size": os.path.getsize(fp)})
    items.sort(key=lambda x: x["name"], reverse=True)
    return items


def _download_archive(job: dict, archive: str, work_dir: str) -> str:
    """Fetch one archive from the destination into work_dir. Returns local path."""
    if "/" in archive or archive.startswith("."):
        raise BackupError("نام آرشیو نامعتبر / invalid archive name")
    local = os.path.join(work_dir, archive)
    dest = job.get("dest_type", "ftp")
    if dest == "ftp":
        with ftplib.FTP() as ftp:
            ftp.connect(job["dest_host"], int(job.get("dest_port") or 21), timeout=120)
            ftp.login(job["dest_user"], job["dest_password"])
            _ftp_chdir(ftp, job.get("dest_path") or "/")
            with open(local, "wb") as fh:
                ftp.retrbinary(f"RETR {archive}", fh.write)
    elif dest == "sftp":
        import paramiko
        transport = paramiko.Transport((job["dest_host"], int(job.get("dest_port") or 22)))
        transport.connect(username=job["dest_user"], password=job["dest_password"])
        try:
            sftp = paramiko.SFTPClient.from_transport(transport)
            remote = (job.get("dest_path") or "/").rstrip("/") + "/" + archive
            sftp.get(remote, local)
        finally:
            transport.close()
    elif dest == "local":
        import shutil
        src = os.path.join(job["dest_path"], archive)
        if not os.path.isfile(src):
            raise BackupError("آرشیو یافت نشد / archive not found")
        shutil.copy2(src, local)
    else:
        raise BackupError(f"مقصد نامعتبر / invalid dest_type: {dest}")
    if not os.path.isfile(local):
        raise BackupError("دانلود آرشیو ناموفق بود / archive download failed")
    return local


def restore_job(job: dict, archive: str, apply: bool = True) -> dict:
    """Download an archive from the destination and restore it to the source.

    site/path  -> extract tar.gz into the target directory
    database   -> pipe the .sql.gz into the mysql client
    """
    status = {"job": job.get("name"), "archive": archive,
              "source": f"{job['source_type']}:{job['source_ref']}", "applied": False}
    with tempfile.TemporaryDirectory() as tmp:
        local = _download_archive(job, archive, tmp)
        status["size_bytes"] = os.path.getsize(local)
        if not apply:
            status["downloaded"] = True
            return status

        stype = job["source_type"]
        if stype in ("site", "path"):
            if stype == "site":
                site = get_site(job["source_ref"])
                if not site:
                    raise BackupError(f"سایت یافت نشد / site not found: {job['source_ref']}")
                target = site["webroot"]
            else:
                target = job["source_ref"]
            os.makedirs(target, exist_ok=True)
            with tarfile.open(local, "r:gz") as tar:
                base = os.path.realpath(target)
                for m in tar.getmembers():
                    dest_p = os.path.realpath(os.path.join(target, m.name))
                    if not (dest_p == base or dest_p.startswith(base + os.sep)):
                        raise BackupError("آرشیو حاوی مسیر ناامن است / unsafe path in archive")
                tar.extractall(target)  # noqa: S202 — members validated above
            status.update(restored_to=target, applied=True)

        elif stype == "database":
            if not oscmd.has("mysql"):
                raise BackupError("mysql client نصب نیست / mysql client not installed")
            import gzip
            import subprocess
            sql = gzip.open(local, "rt", encoding="utf-8").read()
            r = subprocess.run(["mysql", job["source_ref"]], input=sql,
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise BackupError(f"بازیابی دیتابیس ناموفق / db restore failed: {r.stderr[:300]}")
            status.update(restored_to=job["source_ref"], applied=True)
        else:
            raise BackupError(f"نوع منبع نامعتبر / invalid source_type: {stype}")

    db.audit(None, "backup.restore", f"{status['source']} <- {archive}")
    return status


# --------------------------------------------------------------------------- #
# Job CRUD
# --------------------------------------------------------------------------- #
def create_job(data: dict) -> dict:
    fields = ("name", "source_type", "source_ref", "dest_type", "dest_host", "dest_port",
              "dest_user", "dest_password", "dest_path", "schedule_cron", "retention")
    vals = {k: data.get(k) for k in fields}
    with db.get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO backup_jobs ({','.join(fields)}) VALUES ({','.join('?'*len(fields))})",
            tuple(vals[k] for k in fields),
        )
        job_id = cur.lastrowid
    db.audit(None, "backup.create_job", vals["name"] or "")
    return {"id": job_id, **vals}


def list_jobs() -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM backup_jobs ORDER BY created_at DESC")]


def get_job(job_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM backup_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def delete_job(job_id: int) -> dict:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM backup_jobs WHERE id=?", (job_id,))
    db.audit(None, "backup.delete_job", str(job_id))
    return {"id": job_id, "deleted": True}
