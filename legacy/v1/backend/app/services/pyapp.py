"""Python/Django app deployment from Git — venv + gunicorn/uvicorn + nginx proxy.

استقرار اپ پایتون/جنگو از گیت: کلون مخزن، ساخت venv، نصب requirements، اجرای
gunicorn (WSGI) یا uvicorn (ASGI) به‌صورت سرویس systemd، و پروکسی nginx به آن.
redeploy یک‌کلیکی: git pull + نصب + migrate + collectstatic + restart.

طراحی امن: کلون/venv/pip زیر مسیر متعلق به کاربر پنل اجرا می‌شوند (بدون root)؛ فقط
نوشتن یونیت systemd و کنترل سرویس با sudo انجام می‌شود. سرویس با کاربر پنل اجرا
می‌شود تا به venv دسترسی داشته باشد.
"""
from __future__ import annotations

import os
import re
import hmac
import hashlib
import getpass
import secrets
import logging
import threading
import subprocess

from app.core import oscmd
from app.services import sites
from app import db

log = logging.getLogger("icsd.pyapp")

APP_BASE = os.environ.get("ICSD_PYAPP_BASE", "/srv/pyapps")
PORT_MIN, PORT_MAX = 8001, 8999
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
_VALID_TYPES = {"django", "wsgi", "asgi"}


class PyAppError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def validate_name(name: str) -> str:
    name = name.strip().lower()
    if not _NAME_RE.match(name):
        raise PyAppError("نام نامعتبر (a-z,0-9,_,-) / invalid name")
    return name


def _panel_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa
        return "icsdpanel"


def _pick_port() -> int:
    used = {a["port"] for a in list_apps()}
    for p in range(PORT_MIN, PORT_MAX + 1):
        if p not in used:
            return p
    raise PyAppError("پورت آزاد یافت نشد / no free port")


def _python_bin(version: str | None) -> str:
    """Resolve a python interpreter (e.g. '3.11' -> python3.11, fallback python3)."""
    if version:
        cand = f"python{version}"
        if oscmd.has(cand):
            return cand
    return "python3" if oscmd.has("python3") else "python"


def _ensure_base() -> None:
    """Create APP_BASE owned by the panel user (needs root once)."""
    if not os.path.isdir(APP_BASE):
        oscmd.run_priv(["mkdir", "-p", APP_BASE])
        oscmd.run_priv(["chown", f"{_panel_user()}:{_panel_user()}", APP_BASE])


def _write_unit(service_name: str, content: str) -> None:
    """Write a systemd unit file with privilege (via sudo tee)."""
    dest = f"/etc/systemd/system/{service_name}"
    proc = subprocess.run(oscmd.sudo_prefix() + ["tee", dest], input=content,
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise PyAppError(f"نوشتن سرویس ناموفق / write unit failed: {proc.stderr[:200]}")


def render_unit(name: str, app_dir: str, venv: str, app_type: str,
                entry: str, port: int, user: str, has_env: bool) -> str:
    """Render the systemd unit. Pure function — testable."""
    if app_type == "asgi":
        exec_start = (f"{venv}/bin/uvicorn {entry} --host 127.0.0.1 --port {port} "
                      f"--workers 3")
    else:  # django / wsgi both use gunicorn
        exec_start = (f"{venv}/bin/gunicorn {entry} --bind 127.0.0.1:{port} "
                      f"--workers 3 --timeout 60")
    env_line = f"EnvironmentFile={app_dir}/.env.icsd\n" if has_env else ""
    return f"""[Unit]
Description=ICSD PyApp - {name}
After=network.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={app_dir}
{env_line}ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _run_in(app_dir: str, args: list[str], timeout: int = 600) -> oscmd.CmdResult:
    """Run a command inside the app directory (as the panel user, no sudo)."""
    try:
        proc = subprocess.run(args, cwd=app_dir, capture_output=True, text=True, timeout=timeout)
        return oscmd.CmdResult(proc.returncode == 0, proc.returncode,
                               proc.stdout.strip(), proc.stderr.strip())
    except FileNotFoundError:
        return oscmd.CmdResult(False, 127, "", f"command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        return oscmd.CmdResult(False, 124, "", f"timeout after {timeout}s")


# --------------------------------------------------------------------------- #
# Build steps (shared by create + deploy)
# --------------------------------------------------------------------------- #
def _install_deps(app_dir: str, venv: str, app_type: str) -> list[str]:
    log: list[str] = []
    pip = f"{venv}/bin/pip"
    r = _run_in(app_dir, [pip, "install", "--upgrade", "pip"])
    log.append("pip upgrade: " + ("ok" if r.ok else r.stderr[:120]))
    req = os.path.join(app_dir, "requirements.txt")
    if os.path.isfile(req):
        r = _run_in(app_dir, [pip, "install", "-r", "requirements.txt"])
        log.append("requirements: " + ("ok" if r.ok else r.stderr[:200]))
        if not r.ok:
            raise PyAppError(f"نصب requirements ناموفق / pip install failed: {r.stderr[:200]}")
    # always ensure the app server is present
    server = "uvicorn" if app_type == "asgi" else "gunicorn"
    r = _run_in(app_dir, [pip, "install", server])
    log.append(f"{server}: " + ("ok" if r.ok else r.stderr[:120]))
    return log


def _django_steps(app_dir: str, venv: str) -> list[str]:
    log: list[str] = []
    py = f"{venv}/bin/python"
    if not os.path.isfile(os.path.join(app_dir, "manage.py")):
        return ["django: manage.py not found, skipped"]
    r = _run_in(app_dir, [py, "manage.py", "migrate", "--noinput"])
    log.append("migrate: " + ("ok" if r.ok else r.stderr[:200]))
    r = _run_in(app_dir, [py, "manage.py", "collectstatic", "--noinput"])
    log.append("collectstatic: " + ("ok" if r.ok else r.stderr[:160]))
    return log


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
def create_app(name: str, domain: str, repo_url: str, *, branch: str = "main",
               app_type: str = "django", entry: str = "", env_vars: str = "",
               python_version: str | None = None, apply: bool = True) -> dict:
    name = validate_name(name)
    if app_type not in _VALID_TYPES:
        raise PyAppError(f"نوع اپ نامعتبر / invalid app_type: {app_type}")
    if domain:
        domain = sites.validate_domain(domain)
    if get_app_by_name(name):
        raise PyAppError("اپی با این نام وجود دارد / app name already exists")
    if not re.match(r"^https?://", repo_url) and not repo_url.startswith("git@"):
        raise PyAppError("آدرس مخزن نامعتبر / invalid repo URL")

    app_dir = os.path.join(APP_BASE, name)
    venv = os.path.join(app_dir, "venv")
    port = _pick_port()
    service_name = f"icsd-pyapp-{name}.service"
    if not entry:
        entry = "app:app" if app_type == "asgi" else f"{name}.wsgi:application"

    plan = {"name": name, "domain": domain, "repo_url": repo_url, "branch": branch,
            "app_type": app_type, "entry": entry, "port": port, "app_dir": app_dir,
            "venv": venv, "service": service_name, "applied": apply}
    if not apply:
        return plan

    _ensure_base()
    if os.path.exists(app_dir):
        raise PyAppError("پوشهٔ اپ از قبل وجود دارد / app dir already exists")

    # 1) git clone
    pybin = _python_bin(python_version)
    r = _run_in(APP_BASE, ["git", "clone", "--branch", branch, "--depth", "1", repo_url, name])
    if not r.ok:
        # retry without branch (repo default branch may differ)
        r = _run_in(APP_BASE, ["git", "clone", "--depth", "1", repo_url, name])
        if not r.ok:
            raise PyAppError(f"کلون گیت ناموفق / git clone failed: {r.stderr[:200]}")

    # 2) venv
    r = _run_in(app_dir, [pybin, "-m", "venv", "venv"])
    if not r.ok:
        raise PyAppError(f"ساخت venv ناموفق / venv failed: {r.stderr[:200]}")

    # 3) env file
    has_env = bool(env_vars.strip())
    if has_env:
        open(os.path.join(app_dir, ".env.icsd"), "w", encoding="utf-8").write(
            env_vars.strip() + "\n")

    # 4) deps + django
    build_log = _install_deps(app_dir, venv, app_type)
    if app_type == "django":
        build_log += _django_steps(app_dir, venv)

    # 5) systemd unit
    unit = render_unit(name, app_dir, venv, app_type, entry, port, _panel_user(), has_env)
    _write_unit(service_name, unit)
    oscmd.run_priv(["systemctl", "daemon-reload"])
    oscmd.run_priv(["systemctl", "enable", "--now", service_name])

    # 6) nginx reverse proxy site
    if domain:
        try:
            sites.create_site(domain=domain, site_type="proxy",
                              proxy_pass=f"http://127.0.0.1:{port}",
                              webroot=app_dir, apply=True)
        except Exception as e:  # noqa
            build_log.append(f"nginx site: failed ({e})")

    # 7) record
    secret = _insert_app(name, domain, repo_url, branch, app_type, entry, port,
                         app_dir, venv, service_name)
    db.audit(None, "pyapp.create", f"{name} <- {repo_url}")
    return {**plan, "created": True, "build_log": build_log, "webhook_secret": secret}


def _insert_app(name, domain, repo_url, branch, app_type, entry, port,
                app_dir, venv, service_name) -> str:
    """Insert a pyapp row with a fresh webhook secret. Returns the secret."""
    secret = secrets.token_hex(20)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO pyapps (name, domain, repo_url, branch, app_type, entry, "
            "port, app_dir, venv_dir, service_name, webhook_secret, last_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, domain, repo_url, branch, app_type, entry, port, app_dir, venv,
             service_name, secret, "deployed"),
        )
    return secret


# --------------------------------------------------------------------------- #
# Deploy (update)
# --------------------------------------------------------------------------- #
def deploy(app_id: int) -> dict:
    app = get_app(app_id)
    if not app:
        raise PyAppError("اپ یافت نشد / app not found")
    app_dir, venv = app["app_dir"], app["venv_dir"]
    log: list[str] = []

    r = _run_in(app_dir, ["git", "pull", "--ff-only"])
    log.append("git pull: " + (r.stdout[:120] if r.ok else r.stderr[:160]))
    if not r.ok:
        _set_status(app_id, "pull failed")
        raise PyAppError(f"git pull ناموفق / pull failed: {r.stderr[:200]}")

    log += _install_deps(app_dir, venv, app["app_type"])
    if app["app_type"] == "django":
        log += _django_steps(app_dir, venv)

    res = oscmd.run_priv(["systemctl", "restart", app["service_name"]])
    log.append("restart: " + ("ok" if res.ok else res.stderr[:160]))
    _set_status(app_id, "deployed" if res.ok else "restart failed")
    db.audit(None, "pyapp.deploy", app["name"])
    return {"name": app["name"], "deployed": True, "build_log": log}


def control(app_id: int, action: str) -> dict:
    if action not in ("start", "stop", "restart", "status"):
        raise PyAppError("عملیات نامعتبر / invalid action")
    app = get_app(app_id)
    if not app:
        raise PyAppError("اپ یافت نشد / app not found")
    res = oscmd.run_priv(["systemctl", action, app["service_name"]])
    if action == "status":
        return {"name": app["name"], "status": res.stdout[-1500:] or res.stderr[-1500:]}
    _set_status(app_id, action + "ed")
    return {"name": app["name"], "action": action, "ok": res.ok,
            "detail": (res.stderr[:200] if not res.ok else "")}


def logs(app_id: int, lines: int = 200) -> dict:
    app = get_app(app_id)
    if not app:
        raise PyAppError("اپ یافت نشد / app not found")
    lines = max(1, min(int(lines), 1000))
    if not oscmd.has("journalctl"):
        return {"name": app["name"], "content": "journalctl not available"}
    res = oscmd.run_priv(["journalctl", "-u", app["service_name"], "-n", str(lines),
                          "--no-pager", "--no-hostname"])
    return {"name": app["name"], "content": res.stdout or res.stderr or "—"}


def delete_app(app_id: int, remove_files: bool = False) -> dict:
    app = get_app(app_id)
    if not app:
        raise PyAppError("اپ یافت نشد / app not found")
    oscmd.run_priv(["systemctl", "disable", "--now", app["service_name"]])
    oscmd.run_priv(["rm", "-f", f"/etc/systemd/system/{app['service_name']}"])
    oscmd.run_priv(["systemctl", "daemon-reload"])
    if app.get("domain"):
        try:
            sites.delete_site(app["domain"], apply=True)
        except Exception:  # noqa
            pass
    if remove_files and app["app_dir"].startswith(APP_BASE):
        oscmd.run_priv(["rm", "-rf", app["app_dir"]])
    with db.get_conn() as conn:
        conn.execute("DELETE FROM pyapps WHERE id=?", (app_id,))
    db.audit(None, "pyapp.delete", app["name"])
    return {"name": app["name"], "deleted": True}


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def _set_status(app_id: int, status: str) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE pyapps SET last_status=?, last_deploy=datetime('now') WHERE id=?",
                     (status[:200], app_id))


def get_app(app_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM pyapps WHERE id=?", (app_id,)).fetchone()
    return dict(row) if row else None


def get_app_by_name(name: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM pyapps WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def list_apps() -> list[dict]:
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM pyapps ORDER BY created_at DESC")]
    # never expose the webhook secret in list views
    for r in rows:
        r.pop("webhook_secret", None)
    return rows


# --------------------------------------------------------------------------- #
# Ready-made templates (scaffold locally — no external repo needed)
# --------------------------------------------------------------------------- #
TEMPLATES = {
    "fastapi": {"label": "FastAPI", "app_type": "asgi", "entry": "main:app",
                "desc": "API مدرن و سریع (ASGI/uvicorn)"},
    "flask":   {"label": "Flask", "app_type": "wsgi", "entry": "app:app",
                "desc": "میکروفریم‌ورک سبک (WSGI/gunicorn)"},
    "django":  {"label": "Django", "app_type": "django", "entry": "{proj}.wsgi:application",
                "desc": "فریم‌ورک کامل (ORM/Admin/collectstatic)"},
}

_FASTAPI_MAIN = '''from fastapi import FastAPI

app = FastAPI(title="ICSD FastAPI app")


@app.get("/")
def root():
    return {"status": "ok", "framework": "fastapi", "powered_by": "ICSD Panel"}
'''
_FLASK_APP = '''from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/")
def index():
    return jsonify(status="ok", framework="flask", powered_by="ICSD Panel")


if __name__ == "__main__":
    app.run()
'''


def list_templates() -> list[dict]:
    return [{"id": k, **v} for k, v in TEMPLATES.items()]


def create_from_template(name: str, domain: str, template: str, *,
                         env_vars: str = "", python_version: str | None = None,
                         apply: bool = True) -> dict:
    name = validate_name(name)
    if template not in TEMPLATES:
        raise PyAppError(f"قالب نامعتبر / invalid template: {template}")
    tpl = TEMPLATES[template]
    if domain:
        domain = sites.validate_domain(domain)
    if get_app_by_name(name):
        raise PyAppError("اپی با این نام وجود دارد / app name already exists")

    app_dir = os.path.join(APP_BASE, name)
    venv = os.path.join(app_dir, "venv")
    port = _pick_port()
    service_name = f"icsd-pyapp-{name}.service"
    app_type = tpl["app_type"]
    proj = re.sub(r"[^a-z0-9_]", "_", name)
    if proj[0].isdigit():
        proj = "app_" + proj
    entry = tpl["entry"].format(proj=proj)

    plan = {"name": name, "domain": domain, "template": template, "app_type": app_type,
            "entry": entry, "port": port, "app_dir": app_dir, "applied": apply}
    if not apply:
        return plan

    _ensure_base()
    if os.path.exists(app_dir):
        raise PyAppError("پوشهٔ اپ از قبل وجود دارد / app dir already exists")
    os.makedirs(app_dir, exist_ok=True)
    pybin = _python_bin(python_version)
    build_log: list[str] = []

    r = _run_in(app_dir, [pybin, "-m", "venv", "venv"])
    if not r.ok:
        raise PyAppError(f"ساخت venv ناموفق / venv failed: {r.stderr[:200]}")

    has_env = bool(env_vars.strip())
    if has_env:
        open(os.path.join(app_dir, ".env.icsd"), "w", encoding="utf-8").write(env_vars.strip() + "\n")

    if template == "fastapi":
        open(os.path.join(app_dir, "main.py"), "w", encoding="utf-8").write(_FASTAPI_MAIN)
        open(os.path.join(app_dir, "requirements.txt"), "w", encoding="utf-8").write(
            "fastapi\nuvicorn[standard]\n")
        build_log += _install_deps(app_dir, venv, app_type)
    elif template == "flask":
        open(os.path.join(app_dir, "app.py"), "w", encoding="utf-8").write(_FLASK_APP)
        open(os.path.join(app_dir, "requirements.txt"), "w", encoding="utf-8").write(
            "flask\ngunicorn\n")
        build_log += _install_deps(app_dir, venv, app_type)
    elif template == "django":
        open(os.path.join(app_dir, "requirements.txt"), "w", encoding="utf-8").write(
            "django\ngunicorn\n")
        build_log += _install_deps(app_dir, venv, app_type)
        r = _run_in(app_dir, [f"{venv}/bin/django-admin", "startproject", proj, "."])
        if not r.ok:
            raise PyAppError(f"startproject ناموفق / django startproject failed: {r.stderr[:200]}")
        # make the fresh project servable behind the proxy
        settings_py = os.path.join(app_dir, proj, "settings.py")
        try:
            with open(settings_py, "a", encoding="utf-8") as f:
                f.write("\n# --- added by ICSD Panel ---\n"
                        "ALLOWED_HOSTS = ['*']\n"
                        "import os as _os\n"
                        "STATIC_ROOT = _os.path.join(BASE_DIR, 'staticfiles')\n")
        except OSError as e:
            build_log.append(f"settings patch failed: {e}")
        build_log += _django_steps(app_dir, venv)

    unit = render_unit(name, app_dir, venv, app_type, entry, port, _panel_user(), has_env)
    _write_unit(service_name, unit)
    oscmd.run_priv(["systemctl", "daemon-reload"])
    oscmd.run_priv(["systemctl", "enable", "--now", service_name])

    if domain:
        try:
            sites.create_site(domain=domain, site_type="proxy",
                              proxy_pass=f"http://127.0.0.1:{port}",
                              webroot=app_dir, apply=True)
        except Exception as e:  # noqa
            build_log.append(f"nginx site: failed ({e})")

    secret = _insert_app(name, domain, f"template:{template}", "-", app_type, entry,
                         port, app_dir, venv, service_name)
    db.audit(None, "pyapp.template", f"{name} <- {template}")
    return {**plan, "created": True, "build_log": build_log, "webhook_secret": secret}


# --------------------------------------------------------------------------- #
# GitHub webhook — verify signature and redeploy on push
# --------------------------------------------------------------------------- #
def get_webhook_info(app_id: int) -> dict:
    app = get_app(app_id)
    if not app:
        raise PyAppError("اپ یافت نشد / app not found")
    return {"app_id": app_id, "name": app["name"], "branch": app["branch"],
            "path": f"/api/pyapps/{app_id}/webhook",
            "secret": app.get("webhook_secret") or "",
            "content_type": "application/json", "events": "push"}


def regenerate_secret(app_id: int) -> dict:
    app = get_app(app_id)
    if not app:
        raise PyAppError("اپ یافت نشد / app not found")
    secret = secrets.token_hex(20)
    with db.get_conn() as conn:
        conn.execute("UPDATE pyapps SET webhook_secret=? WHERE id=?", (secret, app_id))
    db.audit(None, "pyapp.regen_secret", app["name"])
    return {"app_id": app_id, "secret": secret}


def verify_signature(secret: str | None, body: bytes, signature_header: str | None) -> bool:
    """Validate GitHub's X-Hub-Signature-256 HMAC over the raw body."""
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    sent = signature_header.split("=", 1)[1]
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, sent)


def _safe_deploy(app_id: int) -> None:
    try:
        deploy(app_id)
    except Exception as e:  # noqa
        log.error("webhook deploy failed for app %s: %s", app_id, e)


def handle_webhook(app_id: int, body: bytes, signature: str | None,
                   event: str | None, ref: str | None) -> dict:
    app = get_app(app_id)
    if not app:
        raise PyAppError("اپ یافت نشد / app not found")
    if not verify_signature(app.get("webhook_secret"), body, signature):
        raise PyAppError("امضای webhook نامعتبر / invalid webhook signature")
    if event == "ping":
        return {"pong": True, "app": app["name"]}
    if event and event != "push":
        return {"ignored_event": event}
    branch = app.get("branch") or "main"
    if ref and not (ref == branch or ref.endswith("/" + branch)):
        return {"ignored_ref": ref, "expected": branch}
    threading.Thread(target=_safe_deploy, args=(app_id,), daemon=True).start()
    _set_status(app_id, "deploy queued (webhook)")
    db.audit(None, "pyapp.webhook", f"{app['name']} ref={ref}")
    return {"deploying": True, "app": app["name"]}
