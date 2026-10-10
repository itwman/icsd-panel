"""Server troubleshooter — automated health checks with suggested fixes.

عیب‌یاب سرور: بررسی خودکار سلامت سیستم با راهکار پیشنهادی برای هر مشکل.
هر بررسی یک «یافته» برمی‌گرداند: شدت (ok/warn/crit)، عنوان، توضیح و راهکار — دوزبانه.
همهٔ بررسی‌ها best-effort هستند: نبودِ ابزار یا خطای یک بررسی بقیه را متوقف نمی‌کند.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import psutil

from app.core import oscmd
from app import db

# Services worth watching when their unit exists on the host.
WATCHED_SERVICES = [
    "nginx", "apache2", "mariadb", "mysql", "postgresql",
    "php8.1-fpm", "php8.2-fpm", "php8.3-fpm", "php-fpm",
    "postfix", "dovecot", "fail2ban", "named", "bind9", "vsftpd",
]

OK, WARN, CRIT = "ok", "warn", "crit"


def _f(check: str, severity: str, fa: str, en: str,
       detail: str = "", fix_fa: str = "", fix_en: str = "") -> dict:
    return {"check": check, "severity": severity,
            "title": {"fa": fa, "en": en}, "detail": detail,
            "fix": {"fa": fix_fa, "en": fix_en}}


# --------------------------------------------------------------------------- #
# Individual checks — each returns list[dict] and must never raise.
# --------------------------------------------------------------------------- #
def check_disks() -> list[dict]:
    out = []
    seen = set()
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint in seen or part.fstype in ("squashfs", "tmpfs", "devtmpfs"):
            continue
        seen.add(part.mountpoint)
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        pct = u.percent
        sev = CRIT if pct >= 95 else WARN if pct >= 85 else OK
        gb = u.free / 1024 ** 3
        out.append(_f("disk", sev,
                      f"فضای دیسک {part.mountpoint}", f"Disk space {part.mountpoint}",
                      f"{pct:.0f}% used — {gb:.1f} GB free",
                      "فایل‌های لاگ و بک‌آپ قدیمی را پاک کنید: journalctl --vacuum-size=200M و بخش بک‌آپ‌ها",
                      "Clean old logs/backups: journalctl --vacuum-size=200M; prune old backups"))
    return out


def check_inodes() -> list[dict]:
    out = []
    try:
        st = os.statvfs("/")
        if st.f_files:
            pct = 100 * (1 - st.f_ffree / st.f_files)
            sev = CRIT if pct >= 95 else WARN if pct >= 85 else OK
            out.append(_f("inodes", sev, "اینودهای ریشه", "Root inodes",
                          f"{pct:.0f}% used",
                          "تعداد زیادی فایل کوچک دارید؛ کش‌ها و session های قدیمی را پاک کنید",
                          "Too many small files; clear caches and stale session files"))
    except (OSError, AttributeError):
        pass
    return out


def check_memory() -> list[dict]:
    out = []
    m = psutil.virtual_memory()
    sev = CRIT if m.percent >= 97 else WARN if m.percent >= 90 else OK
    out.append(_f("memory", sev, "حافظهٔ RAM", "Memory (RAM)",
                  f"{m.percent:.0f}% used — {m.available / 1024 ** 3:.1f} GB available",
                  "سرویس‌های پرمصرف را با top بیابید؛ در صورت نیاز swap اضافه کنید",
                  "Find heavy services with top; consider adding swap"))
    s = psutil.swap_memory()
    if s.total:
        sev = WARN if s.percent >= 80 else OK
        out.append(_f("swap", sev, "حافظهٔ Swap", "Swap",
                      f"{s.percent:.0f}% used",
                      "مصرف بالای swap یعنی کمبود RAM — سرویس‌ها را سبک کنید یا RAM بیفزایید",
                      "High swap usage means RAM pressure — trim services or add RAM"))
    return out


def check_load() -> list[dict]:
    try:
        load1, load5, _ = os.getloadavg()
    except (OSError, AttributeError):
        return []
    n = psutil.cpu_count() or 1
    ratio = load5 / n
    sev = CRIT if ratio > 2 else WARN if ratio > 1 else OK
    return [_f("load", sev, "بار پردازنده", "CPU load",
               f"load 1m={load1:.2f}, 5m={load5:.2f} on {n} cores",
               "پردازش‌های سنگین را با top/htop شناسایی کنید",
               "Identify heavy processes with top/htop")]


def check_failed_units() -> list[dict]:
    if not oscmd.has("systemctl"):
        return []
    r = oscmd.run(["systemctl", "--failed", "--no-legend", "--plain"], timeout=15)
    if not r.ok and not r.stdout:
        return []
    failed = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    if not failed:
        return [_f("units", OK, "سرویس‌های systemd", "systemd units", "no failed units")]
    return [_f("units", CRIT, "سرویس‌های خراب systemd", "Failed systemd units",
               ", ".join(failed[:10]),
               f"وضعیت را ببینید: systemctl status {failed[0]} — سپس restart کنید",
               f"Inspect: systemctl status {failed[0]} — then restart it")]


def check_services() -> list[dict]:
    if not oscmd.has("systemctl"):
        return []
    out = []
    for svc in WATCHED_SERVICES:
        ex = oscmd.run(["systemctl", "list-unit-files", f"{svc}.service",
                        "--no-legend", "--plain"], timeout=10)
        if not ex.stdout.strip():
            continue  # not installed
        if "masked" in ex.stdout:
            continue
        act = oscmd.run(["systemctl", "is-active", svc], timeout=10)
        state = act.stdout.strip() or "unknown"
        if state == "active":
            out.append(_f(f"svc:{svc}", OK, f"سرویس {svc}", f"Service {svc}", "active"))
        else:
            en_st = oscmd.run(["systemctl", "is-enabled", svc], timeout=10).stdout.strip()
            sev = CRIT if en_st == "enabled" else WARN
            out.append(_f(f"svc:{svc}", sev, f"سرویس {svc} خاموش است", f"Service {svc} is down",
                          f"state={state}, enabled={en_st or 'unknown'}",
                          f"راه‌اندازی: systemctl start {svc} — و لاگ: journalctl -u {svc} -e",
                          f"Start it: systemctl start {svc} — logs: journalctl -u {svc} -e"))
    return out


def check_nginx_config() -> list[dict]:
    if not oscmd.has("nginx"):
        return []
    r = oscmd.run_priv(["nginx", "-t"], timeout=15)
    if r.ok:
        return [_f("nginx-t", OK, "کانفیگ Nginx", "Nginx config", "syntax ok")]
    return [_f("nginx-t", CRIT, "کانفیگ Nginx خطا دارد", "Nginx config is broken",
               (r.stderr or r.stdout)[:400],
               "خط خطادار را در پیام بالا ببینید و فایل کانفیگ را اصلاح کنید؛ سپس nginx -t و reload",
               "Fix the file/line shown above, re-run nginx -t, then reload")]


def check_ssl_expiry() -> list[dict]:
    try:
        from app.services import certs
        scan = certs.scan_server()
    except Exception:
        return []
    out = []
    for c in scan.get("certificates", [])[:20]:
        days = c.get("days_left")
        if days is None:
            continue
        name = c.get("common_name") or c.get("path", "?")
        if days < 0:
            out.append(_f("ssl", CRIT, f"گواهی منقضی: {name}", f"Expired cert: {name}",
                          f"{-days} days ago — {c.get('path','')}",
                          "از بخش SSL گواهی جدید صادر/تمدید کنید",
                          "Issue/renew from the SSL section"))
        elif days <= 14:
            out.append(_f("ssl", WARN, f"گواهی رو به انقضا: {name}", f"Cert expiring soon: {name}",
                          f"{days} days left — {c.get('path','')}",
                          "از بخش SSL تمدید کنید (Renew all)",
                          "Renew from the SSL section (Renew all)"))
    if not out:
        out.append(_f("ssl", OK, "گواهی‌های SSL", "SSL certificates",
                      "no certificate expiring within 14 days"))
    return out


def check_web_ports() -> list[dict]:
    """If a web server is active, ports 80/443 should be listening."""
    if not (oscmd.has("nginx") or oscmd.has("apache2")):
        return []
    listening = set()
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr:
                listening.add(c.laddr.port)
    except (psutil.AccessDenied, OSError):
        return []
    out = []
    for port in (80, 443):
        if port in listening:
            out.append(_f(f"port:{port}", OK, f"پورت {port}", f"Port {port}", "listening"))
        else:
            out.append(_f(f"port:{port}", WARN, f"پورت {port} باز نیست", f"Port {port} not listening",
                          "no listener found",
                          "وب‌سرور را بررسی/استارت کنید و فایروال را چک کنید",
                          "Check/start the web server and verify the firewall"))
    return out


def check_mail_queue() -> list[dict]:
    if not oscmd.has("postqueue"):
        return []
    r = oscmd.run_priv(["postqueue", "-p"], timeout=20)
    txt = r.stdout or ""
    if "Mail queue is empty" in txt or not txt.strip():
        return [_f("mailq", OK, "صف ایمیل", "Mail queue", "empty")]
    # last line like: "-- 12 Kbytes in 34 Requests."
    n = 0
    for tok in txt.strip().splitlines()[-1].split():
        if tok.isdigit():
            n = int(tok)  # last number = requests
    sev = CRIT if n > 200 else WARN if n > 50 else OK
    return [_f("mailq", sev, "صف ایمیل", "Mail queue", f"{n} queued messages",
               "دلیل گیرکردن را ببینید: postqueue -p و لاگ postfix؛ ارسال مجدد: postqueue -f",
               "Inspect with postqueue -p and postfix logs; flush: postqueue -f")]


def check_time_sync() -> list[dict]:
    if not oscmd.has("timedatectl"):
        return []
    r = oscmd.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=10)
    if r.stdout.strip() == "yes":
        return [_f("ntp", OK, "همگام‌سازی ساعت", "Clock sync (NTP)", "synchronized")]
    return [_f("ntp", WARN, "ساعت سرور همگام نیست", "Server clock not synchronized",
               "NTPSynchronized=no",
               "فعال‌سازی: timedatectl set-ntp true (برای SSL و لاگ‌ها مهم است)",
               "Enable: timedatectl set-ntp true (matters for SSL and logs)")]


def check_reboot_required() -> list[dict]:
    if os.path.exists("/var/run/reboot-required"):
        return [_f("reboot", WARN, "نیاز به ری‌استارت سرور", "Reboot required",
                   "/var/run/reboot-required present (kernel/libc update)",
                   "در زمان کم‌ترافیک سرور را ریبوت کنید: reboot",
                   "Reboot during low traffic: reboot")]
    return []


def check_panel_db() -> list[dict]:
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1")
        return [_f("paneldb", OK, "دیتابیس پنل", "Panel database", "readable/writable")]
    except Exception as e:  # pragma: no cover
        return [_f("paneldb", CRIT, "دیتابیس پنل در دسترس نیست", "Panel database unavailable",
                   str(e)[:200],
                   "مجوز فایل sqlite و فضای دیسک را بررسی کنید",
                   "Check sqlite file permissions and disk space")]


def check_oom() -> list[dict]:
    if not oscmd.has("journalctl"):
        return []
    r = oscmd.run_priv(["journalctl", "-k", "--since", "-24h", "-g", "Out of memory",
                        "--no-pager", "-q"], timeout=20)
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return []
    return [_f("oom", CRIT, "کمبود حافظه (OOM Kill)", "Out-of-memory kills",
               f"{len(lines)} OOM event(s) in last 24h — e.g. {lines[-1][:160]}",
               "RAM/سواپ اضافه کنید یا سرویس پرمصرف را محدود کنید",
               "Add RAM/swap or cap the offending service")]


ALL_CHECKS = [
    check_disks, check_inodes, check_memory, check_load,
    check_failed_units, check_services, check_nginx_config,
    check_ssl_expiry, check_web_ports, check_mail_queue,
    check_time_sync, check_reboot_required, check_panel_db, check_oom,
]


def run_all() -> dict:
    """Run every check (best-effort) and return findings + summary."""
    findings: list[dict] = []
    for chk in ALL_CHECKS:
        try:
            findings.extend(chk())
        except Exception as e:  # a broken check must not kill the report
            findings.append(_f(chk.__name__, WARN, "بررسی ناموفق", "Check failed",
                               f"{chk.__name__}: {e}"))
    order = {CRIT: 0, WARN: 1, OK: 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    summary = {s: sum(1 for f in findings if f["severity"] == s) for s in (OK, WARN, CRIT)}
    db.audit(None, "troubleshoot.run", f"crit={summary[CRIT]} warn={summary[WARN]}")
    return {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": summary, "findings": findings}
