"""Alert rules: when a condition holds long enough, notify once; when it
clears, notify once more. No repeats in between, no flapping storms.

Each (rule, server, subject) has a tiny state: since when it is bad and
whether an event was opened. Open events are reloaded at start-up, so a hub
restart neither re-sends an alert nor forgets to send its «resolved».

قاعده: وقتی شرطی به اندازهٔ کافی ادامه پیدا کرد یک بار هشدار، وقتی رفع شد یک
بار «رفع شد». بینِ این دو تکرار نمی‌شود.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from icsd_hub import db, live, notify

# kind: (default threshold, default seconds, severity, Persian label, unit)
DEFAULT_RULES: dict[str, tuple[float, int, str, str, str]] = {
    "offline":   (120, 0,   "critical", "سرور در دسترس نیست", "ثانیه بی‌خبری"),
    "cpu":       (90,  600, "warning",  "مصرف پردازنده بالا", "٪"),
    "mem":       (90,  300, "warning",  "حافظه رو به اتمام", "٪"),
    "disk":      (85,  0,   "warning",  "دیسک رو به پر شدن", "٪"),
    "disk_full": (95,  0,   "critical", "دیسک تقریباً پر", "٪"),
    "inodes":    (90,  0,   "warning",  "inode رو به اتمام", "٪"),
    "load":      (2.0, 600, "warning",  "بار سیستم بالا", "به ازای هر هسته"),
    "steal":     (20,  600, "warning",  "CPU steal بالا (همسایه‌های VPS)", "٪"),
    "container": (0,   60,  "critical", "کانتینر از کار افتاده", ""),
    "check":     (2,   0,   "critical", "آدرس پاسخ نمی‌دهد", "شکستِ پیاپی"),
    "cert":      (14,  0,   "warning",  "گواهی SSL نزدیک انقضا", "روز"),
}
SERVER_KINDS = ("cpu", "mem", "disk", "disk_full", "inodes", "load", "steal", "container")
STARTUP_GRACE = 90


@dataclass
class Rule:
    kind: str
    threshold: float
    for_s: int
    severity: str
    enabled: bool

    @property
    def label(self) -> str:
        return DEFAULT_RULES[self.kind][3]


_rules: dict[str, Rule] = {}
_state: dict[tuple[str, str, str], dict] = {}
_started = time.time()


def seed() -> None:
    for kind, (thr, for_s, sev, _, _) in DEFAULT_RULES.items():
        db.x("INSERT OR IGNORE INTO rules(kind, threshold, for_s, severity) VALUES(?,?,?,?)",
             (kind, thr, for_s, sev))
    reload()
    _state.clear()
    for e in db.q("SELECT id, rule, server_id, subject, started, value FROM events WHERE ended IS NULL"):
        _state[(e["rule"], e["server_id"], e["subject"])] = {
            "since": e["started"], "event": e["id"], "value": e["value"]}


def reload() -> None:
    _rules.clear()
    for r in db.q("SELECT * FROM rules"):
        if r["kind"] in DEFAULT_RULES:
            _rules[r["kind"]] = Rule(r["kind"], r["threshold"], r["for_s"], r["severity"], bool(r["enabled"]))


def rules_view() -> list[dict]:
    return [{"kind": r.kind, "threshold": r.threshold, "for_s": r.for_s, "severity": r.severity,
             "enabled": r.enabled, "label": r.label, "unit": DEFAULT_RULES[r.kind][4]}
            for r in sorted(_rules.values(), key=lambda r: list(DEFAULT_RULES).index(r.kind))]


def update_rule(kind: str, threshold: float, for_s: int, severity: str, enabled: bool) -> None:
    if kind not in DEFAULT_RULES:
        raise ValueError("unknown rule")
    if severity not in ("warning", "critical"):
        raise ValueError("bad severity")
    db.x("UPDATE rules SET threshold=?, for_s=?, severity=?, enabled=? WHERE kind=?",
         (float(threshold), max(0, min(int(for_s), 86400)), severity, int(enabled), kind))
    reload()


# ── the state machine ──────────────────────────────────────────────────

def _server_name(server_id: str) -> str:
    if not server_id:
        return "پایش آدرس‌ها"
    row = db.one("SELECT name FROM servers WHERE id=?", (server_id,))
    return row["name"] if row else server_id


def _duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "کمتر از یک دقیقه"
    if m < 60:
        return f"{m} دقیقه"
    h, m = divmod(m, 60)
    return f"{h} ساعت و {m} دقیقه" if m else f"{h} ساعت"


def step(kind: str, server_id: str, subject: str, bad: bool, value: float, message: str,
         now: float | None = None, quiet: bool = False) -> None:
    rule = _rules.get(kind)
    key = (kind, server_id, subject)
    now = now or time.time()
    st = _state.get(key)
    if rule is None or not rule.enabled:
        bad = False
    if bad:
        if st is None:
            st = _state[key] = {"since": now, "event": None, "value": value}
        st["value"] = value
        if st["event"] is None and now - st["since"] >= rule.for_s:
            st["event"] = db.x(
                "INSERT INTO events(rule, server_id, subject, severity, value, message, started) "
                "VALUES(?,?,?,?,?,?,?)",
                (kind, server_id, subject, rule.severity, value, message, int(st["since"])))
            st["message"] = message
            icon = "🔴" if rule.severity == "critical" else "🟠"
            notify.fire(f"{icon} {rule.label} — {_server_name(server_id)}\n{message}\n🕒 {notify.jalali(now)}")
    elif st is not None:
        if st["event"] is not None:
            db.x("UPDATE events SET ended=? WHERE id=?", (int(now), st["event"]))
            msg = st.get("message") or (db.one("SELECT message FROM events WHERE id=?", (st["event"],))
                                        or {}).get("message", "")
            if not quiet:
                notify.fire(f"🟢 رفع شد: {DEFAULT_RULES[kind][3]} — {_server_name(server_id)}\n{msg}\n"
                            f"⏱ مدت: {_duration(now - st['since'])}")
        del _state[key]


def settle(kinds: tuple[str, ...], server_id: str, seen: set[tuple[str, str]], now: float) -> None:
    """Subjects that vanished (container removed, disk unmounted) are resolved."""
    for key in [k for k in _state if k[0] in kinds and k[1] == server_id and (k[0], k[2]) not in seen]:
        step(key[0], server_id, key[2], False, 0, "", now)


def forget_server(server_id: str) -> None:
    now = int(time.time())
    db.x("UPDATE events SET ended=? WHERE server_id=? AND ended IS NULL", (now, server_id))
    for key in [k for k in _state if k[1] == server_id]:
        del _state[key]


# ── evaluators ─────────────────────────────────────────────────────────

def _fa(v: float, digits: int = 0) -> str:
    return f"{v:.{digits}f}"


def _iso(text: str) -> str:
    """Isolate a Latin path/name so an RTL message (panel, Telegram, Bale)
    does not scramble it: «/opt/data: 91٪» instead of «opt/data: 91٪/»."""
    return f"\u2066{text}\u2069"


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") or n >= 10 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def evaluate_sample(server_id: str, s: dict, cpus: int, now: float | None = None) -> None:
    now = now or time.time()
    seen: set[tuple[str, str]] = set()

    def chk(kind: str, subject: str, value: float, message: str) -> None:
        r = _rules.get(kind)
        seen.add((kind, subject))
        step(kind, server_id, subject, bool(r and value >= r.threshold), value, message, now)

    r = _rules
    cpu = s["cpu"]["pct"]
    chk("cpu", "", cpu, f"پردازنده {_fa(cpu)}٪ — آستانه {_fa(r['cpu'].threshold)}٪")
    mem = s["mem"]["pct"]
    chk("mem", "", mem, f"حافظه {_fa(mem)}٪ — آستانه {_fa(r['mem'].threshold)}٪")
    per_core = s["load"][0] / max(cpus, 1)
    chk("load", "", per_core, f"بار {s['load'][0]:.2f} روی {cpus} هسته ({per_core:.2f} برای هر هسته)")
    steal = s["cpu"]["steal"]
    chk("steal", "", steal, f"steal {_fa(steal)}٪ — ارائه‌دهندهٔ VPS به سرور CPU کافی نمی‌دهد")
    for d in s.get("disks") or []:
        msg = f"دیسک {_iso(d['mount'])}: {_fa(d['pct'])}٪ پر — {_iso(_size(d['free']))} آزاد"
        chk("disk", d["mount"], d["pct"], msg)
        chk("disk_full", d["mount"], d["pct"], msg)
        chk("inodes", d["mount"], d["inodes_pct"], f"دیسک {_iso(d['mount'])}: inode {_fa(d['inodes_pct'])}٪")
    if "containers" in s and not s.get("docker_err"):
        for c in s["containers"]:
            broken = c["state"] in ("restarting", "dead") or c["health"] == "unhealthy" or (
                c["state"] == "exited" and not c["status"].startswith("Exited (0)"))
            seen.add(("container", c["name"]))
            step("container", server_id, c["name"], broken, 0,
                 f"کانتینر {_iso(c['name'])}: {_iso(c['status'] or c['state'])}", now)
        settle(("container",), server_id, seen, now)
    settle(("cpu", "mem", "load", "steal"), server_id, seen, now)
    # Disks arrive every few samples, and not at all in the first samples
    # after a hub restart: only a sample that carries them can resolve them.
    if "disks" in s:
        settle(("disk", "disk_full", "inodes"), server_id, seen, now)


def evaluate_offline(now: float | None = None) -> None:
    now = now or time.time()
    if now - _started < STARTUP_GRACE:
        return  # agents are still reconnecting after a hub restart
    rule = _rules.get("offline")
    for srv in db.q("SELECT id, last_seen FROM servers WHERE secret_hash IS NOT NULL AND last_seen IS NOT NULL"):
        lv = live.peek(srv["id"])
        last = max(srv["last_seen"] or 0, lv.last_ts if lv else 0)
        silent = now - last
        bad = bool(rule) and silent >= rule.threshold and not (lv and lv.status(now) == "online")
        step("offline", srv["id"], "", bad, silent,
             "هیچ گزارشی از agent نرسیده؛ سرور خاموش است، شبکه‌اش قطع است یا agent متوقف شده.", now)


def is_open(kind: str, server_id: str, subject: str) -> bool:
    st = _state.get((kind, server_id, subject))
    return bool(st and st["event"] is not None)


def threshold(kind: str) -> float:
    r = _rules.get(kind)
    return r.threshold if r else DEFAULT_RULES[kind][0]


def evaluate_check(check_id: int, name: str, url: str, fails: int, error: str,
                   cert_days: float | None, now: float | None = None) -> None:
    subject = str(check_id)
    step("check", "", subject, fails >= (_rules["check"].threshold if "check" in _rules else 2),
         fails, f"{name} ({_iso(url)})\n{error}", now)
    if cert_days is not None:
        step("cert", "", subject, cert_days <= (_rules["cert"].threshold if "cert" in _rules else 14),
             cert_days, f"{name}: گواهی {int(cert_days)} روز دیگر منقضی می‌شود", now)


def open_events() -> list[dict]:
    return db.q("SELECT e.*, s.name AS server_name FROM events e "
                "LEFT JOIN servers s ON s.id=e.server_id WHERE e.ended IS NULL ORDER BY e.started DESC")


def history(limit: int = 100, before: int | None = None) -> list[dict]:
    return db.q("SELECT e.*, s.name AS server_name FROM events e "
                "LEFT JOIN servers s ON s.id=e.server_id WHERE e.started<? "
                "ORDER BY e.started DESC LIMIT ?", (before or 2**31, min(limit, 500)))


def firing_count(server_id: str) -> int:
    return sum(1 for (k, sid, _), st in _state.items() if sid == server_id and st["event"] is not None)
