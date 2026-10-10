"""Security module — fail2ban/IDS, IP monitoring, malware/backdoor scanning.

ماژول امنیت: یکپارچگی fail2ban، رصد IP و اتصالات، و اسکن بدافزار/بک‌دور روی فایل‌ها.
"""
from __future__ import annotations

import os
import re
from collections import Counter

import psutil

from app.core import oscmd
from app import db


# --------------------------------------------------------------------------- #
# fail2ban / IDS
# --------------------------------------------------------------------------- #
def fail2ban_status() -> dict:
    """Read fail2ban jails and banned IPs."""
    if not oscmd.has("fail2ban-client"):
        return {"installed": False, "jails": [],
                "hint": "fail2ban نصب نیست — برای محافظت توصیه می‌شود / install fail2ban for protection"}
    res = oscmd.run(["fail2ban-client", "status"], timeout=15)
    jail_names: list[str] = []
    if res.ok:
        m = re.search(r"Jail list:\s*(.+)", res.stdout)
        if m:
            jail_names = [j.strip() for j in m.group(1).split(",") if j.strip()]
    jails = []
    for name in jail_names:
        jr = oscmd.run(["fail2ban-client", "status", name], timeout=15)
        banned, total = [], 0
        if jr.ok:
            bm = re.search(r"Banned IP list:\s*(.*)", jr.stdout)
            if bm:
                banned = [ip for ip in bm.group(1).split() if ip]
            tm = re.search(r"Total banned:\s*(\d+)", jr.stdout)
            if tm:
                total = int(tm.group(1))
        jails.append({"name": name, "currently_banned": banned, "total_banned": total})
    return {"installed": True, "jails": jails}


def ban_ip(jail: str, ip: str) -> dict:
    if not _valid_ip(ip):
        raise SecurityError(f"IP نامعتبر / invalid IP: {ip}")
    res = oscmd.run(["fail2ban-client", "set", jail, "banip", ip], timeout=15)
    db.audit(None, "security.ban_ip", f"{ip} in {jail}")
    return {"jail": jail, "ip": ip, "ok": res.ok, "output": res.stdout or res.stderr}


def unban_ip(jail: str, ip: str) -> dict:
    if not _valid_ip(ip):
        raise SecurityError(f"IP نامعتبر / invalid IP: {ip}")
    res = oscmd.run(["fail2ban-client", "set", jail, "unbanip", ip], timeout=15)
    db.audit(None, "security.unban_ip", f"{ip} in {jail}")
    return {"jail": jail, "ip": ip, "ok": res.ok, "output": res.stdout or res.stderr}


class SecurityError(Exception):
    pass


_IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


def _valid_ip(ip: str) -> bool:
    if not _IP_RE.match(ip or ""):
        return False
    return all(0 <= int(o) <= 255 for o in ip.split("."))


# --------------------------------------------------------------------------- #
# IP / connection monitoring
# --------------------------------------------------------------------------- #
def active_connections(top: int = 20) -> dict:
    """Established connections grouped by remote IP (potential attackers / load)."""
    counter: Counter = Counter()
    established = 0
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == "ESTABLISHED" and c.raddr:
                counter[c.raddr.ip] += 1
                established += 1
    except (psutil.AccessDenied, PermissionError):
        return {"error": "نیازمند دسترسی root / requires root", "connections": []}
    ranked = [{"ip": ip, "connections": n} for ip, n in counter.most_common(top)]
    return {"established_total": established, "unique_ips": len(counter), "top_ips": ranked}


def failed_logins(top: int = 15) -> dict:
    """Parse auth logs for failed SSH login attempts and rank source IPs."""
    candidates = ["/var/log/auth.log", "/var/log/secure"]
    log_path = next((p for p in candidates if os.path.exists(p)), None)
    if not log_path:
        return {"available": False, "hint": "فایل لاگ احراز هویت یافت نشد / auth log not found"}
    counter: Counter = Counter()
    total = 0
    pat = re.compile(r"Failed password.*from (\d{1,3}(?:\.\d{1,3}){3})")
    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = pat.search(line)
                if m:
                    counter[m.group(1)] += 1
                    total += 1
    except OSError:
        return {"available": False, "hint": "عدم دسترسی به لاگ / cannot read log"}
    return {"available": True, "log": log_path, "total_failed": total,
            "top_ips": [{"ip": ip, "attempts": n} for ip, n in counter.most_common(top)]}


# --------------------------------------------------------------------------- #
# Malware / backdoor scanner
# --------------------------------------------------------------------------- #
# Suspicious PHP/shell patterns commonly found in webshells & backdoors.
SIGNATURES = [
    (r"eval\s*\(\s*(base64_decode|gzinflate|str_rot13|gzuncompress)", "obfuscated eval", "high"),
    (r"\b(shell_exec|system|passthru|popen|proc_open)\s*\(", "command execution", "medium"),
    (r"\bassert\s*\(\s*\$_(GET|POST|REQUEST)", "assert injection", "high"),
    (r"\$_(GET|POST|REQUEST|COOKIE)\s*\[.*?\]\s*\(", "dynamic call from input", "high"),
    (r"\b(FilesMan|c99|r57|WSO|b374k|weevely)\b", "known webshell name", "high"),
    (r"preg_replace\s*\(.*?/e", "preg_replace /e exec", "high"),
    (r"base64_decode\s*\(\s*['\"][A-Za-z0-9+/=]{80,}", "large base64 blob", "medium"),
    (r"\bmove_uploaded_file\b.*\$_(FILES)", "unfiltered upload", "low"),
    (r"\\x[0-9a-fA-F]{2}\\x[0-9a-fA-F]{2}\\x[0-9a-fA-F]{2}", "hex-encoded payload", "low"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), desc, sev) for p, desc, sev in SIGNATURES]
_SCAN_EXT = {".php", ".phtml", ".php5", ".inc", ".js", ".asp", ".aspx", ".py", ".sh"}
_MAX_FILE = 3 * 1024 * 1024  # skip files > 3MB


def scan_path(root: str, max_findings: int = 500) -> dict:
    """Recursively scan a directory for backdoor/malware signatures."""
    if not os.path.isdir(root):
        raise SecurityError(f"مسیر معتبر نیست / not a directory: {root}")
    findings: list[dict] = []
    scanned = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _SCAN_EXT:
                continue
            path = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(path) > _MAX_FILE:
                    continue
                with open(path, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError:
                continue
            scanned += 1
            for rx, desc, sev in _COMPILED:
                m = rx.search(content)
                if m:
                    line_no = content[:m.start()].count("\n") + 1
                    findings.append({"file": path, "line": line_no, "pattern": desc,
                                     "severity": sev, "match": m.group(0)[:80]})
                    if len(findings) >= max_findings:
                        break
            if len(findings) >= max_findings:
                break
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda x: sev_rank.get(x["severity"], 9))
    summary = {"files_scanned": scanned, "findings": len(findings),
               "high": sum(1 for f in findings if f["severity"] == "high"),
               "medium": sum(1 for f in findings if f["severity"] == "medium"),
               "low": sum(1 for f in findings if f["severity"] == "low")}
    db.audit(None, "security.scan", f"{root}: {summary['findings']} findings")
    return {"root": root, "summary": summary, "findings": findings}


def scan_all_sites() -> dict:
    """Scan every managed site's webroot."""
    with db.get_conn() as conn:
        roots = [(r["domain"], r["webroot"]) for r in conn.execute("SELECT domain, webroot FROM sites")]
    results = []
    for domain, root in roots:
        if os.path.isdir(root):
            try:
                r = scan_path(root)
                results.append({"domain": domain, **r["summary"]})
            except SecurityError:
                continue
    return {"sites": results}
