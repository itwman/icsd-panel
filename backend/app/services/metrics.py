"""System metrics collection via psutil.

جمع‌آوری متریک سیستم: CPU، RAM، دیسک، شبکه (پهنای باند)، بار سیستم و آپ‌تایم.
"""
from __future__ import annotations

import time
import shutil
import psutil


# Keep last network sample to compute bandwidth (bytes/sec)
_last_net: dict[str, float] = {"ts": 0.0, "sent": 0.0, "recv": 0.0}


def _bandwidth() -> dict:
    """Compute instantaneous network throughput in bytes/sec."""
    counters = psutil.net_io_counters()
    now = time.time()
    sent, recv = float(counters.bytes_sent), float(counters.bytes_recv)

    up = down = 0.0
    if _last_net["ts"]:
        dt = max(now - _last_net["ts"], 1e-6)
        up = max(sent - _last_net["sent"], 0) / dt
        down = max(recv - _last_net["recv"], 0) / dt

    _last_net.update(ts=now, sent=sent, recv=recv)
    return {
        "up_bytes_per_sec": round(up, 2),
        "down_bytes_per_sec": round(down, 2),
        "total_sent": int(sent),
        "total_recv": int(recv),
    }


def cpu_metrics() -> dict:
    return {
        "percent": psutil.cpu_percent(interval=None),
        "cores": psutil.cpu_count(logical=True),
        "physical_cores": psutil.cpu_count(logical=False),
        "load_avg": [round(x, 2) for x in psutil.getloadavg()] if hasattr(psutil, "getloadavg") else None,
    }


def memory_metrics() -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "total": vm.total,
        "used": vm.used,
        "available": vm.available,
        "percent": vm.percent,
        "swap_total": sw.total,
        "swap_used": sw.used,
        "swap_percent": sw.percent,
    }


def disk_metrics() -> list[dict]:
    out = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = shutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        out.append({
            "device": part.device,
            "mountpoint": part.mountpoint,
            "fstype": part.fstype,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        })
    return out


def uptime_seconds() -> int:
    return int(time.time() - psutil.boot_time())


def snapshot() -> dict:
    """Full metrics snapshot for the live dashboard."""
    return {
        "timestamp": int(time.time()),
        "cpu": cpu_metrics(),
        "memory": memory_metrics(),
        "disk": disk_metrics(),
        "network": _bandwidth(),
        "uptime_seconds": uptime_seconds(),
        "process_count": len(psutil.pids()),
    }


def record_history() -> None:
    """Persist a compact metrics row for historical charts. Called by the scheduler."""
    from app import db
    snap = snapshot()
    disks = snap["disk"]
    disk_pct = max((d["percent"] for d in disks), default=0)
    load1 = snap["cpu"]["load_avg"][0] if snap["cpu"].get("load_avg") else None
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO metrics_history (ts, cpu_percent, mem_percent, disk_percent, net_up, net_down, load1)
               VALUES (?,?,?,?,?,?,?)""",
            (snap["timestamp"], snap["cpu"]["percent"], snap["memory"]["percent"],
             disk_pct, snap["network"]["up_bytes_per_sec"], snap["network"]["down_bytes_per_sec"], load1),
        )


def history(hours: int = 24, max_points: int = 500) -> dict:
    """Return downsampled time-series for the given window."""
    from app import db
    since = int(time.time()) - hours * 3600
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM metrics_history WHERE ts >= ? ORDER BY ts ASC", (since,)
        ).fetchall()
    rows = [dict(r) for r in rows]
    # simple downsample
    if len(rows) > max_points:
        step = len(rows) // max_points + 1
        rows = rows[::step]
    return {"hours": hours, "points": len(rows), "series": rows}


def prune_history(keep_days: int = 30) -> int:
    """Delete metric rows older than keep_days. Returns rows deleted."""
    from app import db
    cutoff = int(time.time()) - keep_days * 86400
    with db.get_conn() as conn:
        cur = conn.execute("DELETE FROM metrics_history WHERE ts < ?", (cutoff,))
        return cur.rowcount
