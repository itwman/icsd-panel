"""Live state of every server, kept in memory, plus per-minute rollup to disk.

Each agent sends a sample every few seconds. The last hour of samples stays
in RAM (a ring buffer, ~60 KB per server); every minute one averaged row is
written to SQLite. So the disk sees one small write per server per minute,
no matter how often agents report.

وضعیتِ زندهٔ هر سرور در حافظه؛ هر دقیقه فقط یک ردیفِ میانگین روی دیسک.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from icsd_hub import config, db

# Order of the numbers in each ring point and in the samples tables.
FIELDS = ("cpu", "iowait", "steal", "mem", "swap", "load1", "disk", "rx", "tx", "rd", "wr", "tcp")
RING = 720  # one hour at 5 s

MAX_DISKS, MAX_TOP, MAX_CONTAINERS = 64, 32, 500


def _f(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _s(v: Any, n: int = 200) -> str:
    return str(v)[:n] if v is not None else ""


def clean_sample(raw: dict) -> dict:
    """An agent is trusted to report numbers, nothing more: coerce and cap."""
    cpu = raw.get("cpu") or {}
    mem = raw.get("mem") or {}
    load = raw.get("load") or [0, 0, 0]
    out = {
        "ts": int(_f(raw.get("ts"))),
        "cpu": {k: _f(cpu.get(k)) for k in ("pct", "user", "system", "iowait", "steal")},
        "load": [_f(v) for v in list(load)[:3]] + [0.0] * (3 - len(list(load)[:3])),
        "mem": {k: _f(mem.get(k)) for k in ("total", "used", "available", "cached", "pct",
                                            "swap_total", "swap_used", "swap_pct")},
        "io": {k: _f((raw.get("io") or {}).get(k)) for k in ("read_bps", "write_bps")},
        "net": {k: _f((raw.get("net") or {}).get(k)) for k in ("rx_bps", "tx_bps")},
        "tcp": int(_f(raw.get("tcp"))),
        "procs": int(_f(raw.get("procs"))),
        "uptime": int(_f(raw.get("uptime"))),
    }
    if isinstance(raw.get("disks"), list):
        out["disks"] = [{
            "mount": _s(d.get("mount")), "device": _s(d.get("device")), "fs": _s(d.get("fs"), 20),
            "total": _f(d.get("total")), "used": _f(d.get("used")), "free": _f(d.get("free")),
            "pct": _f(d.get("pct")), "inodes_pct": _f(d.get("inodes_pct")),
        } for d in raw["disks"][:MAX_DISKS] if isinstance(d, dict)]
    if isinstance(raw.get("top"), list):
        out["top"] = [{
            "pid": int(_f(p.get("pid"))), "name": _s(p.get("name"), 64), "user": _s(p.get("user"), 32),
            "cpu": _f(p.get("cpu")), "rss": _f(p.get("rss")),
        } for p in raw["top"][:MAX_TOP] if isinstance(p, dict)]
    if isinstance(raw.get("containers"), list):
        out["containers"] = [{
            "id": _s(c.get("id"), 64), "name": _s(c.get("name"), 128), "image": _s(c.get("image")),
            "state": _s(c.get("state"), 20), "health": _s(c.get("health"), 20),
            "status": _s(c.get("status"), 100), "cpu": _f(c.get("cpu")), "mem": _f(c.get("mem")),
        } for c in raw["containers"][:MAX_CONTAINERS] if isinstance(c, dict)]
    if raw.get("docker_err"):
        out["docker_err"] = _s(raw["docker_err"])
    return out


@dataclass
class Live:
    server_id: str
    ws: Any = None
    ip: str = ""
    version: str = ""
    connected_at: float = 0.0
    last_ts: float = 0.0            # receive time of the latest sample (hub clock)
    last: dict = field(default_factory=dict)
    ring: deque = field(default_factory=lambda: deque(maxlen=RING))
    acc_minute: int = 0
    acc: list[float] = field(default_factory=lambda: [0.0] * len(FIELDS))
    acc_cpu_max: float = 0.0
    acc_n: int = 0
    seen_written: float = 0.0
    caps: dict = field(default_factory=dict)      # what this server allows (from its policy)
    pending: dict = field(default_factory=dict)   # request id -> Future
    send_lock: Any = None                          # asyncio.Lock, created on first use

    @property
    def connected(self) -> bool:
        return self.ws is not None

    def status(self, now: float | None = None) -> str:
        now = now or time.time()
        fresh = now - self.last_ts < config.settings.sample_interval * 3 + 10
        if self.connected and fresh:
            return "online"
        return "offline"


_live: dict[str, Live] = {}


def get(server_id: str) -> Live:
    lv = _live.get(server_id)
    if lv is None:
        lv = _live[server_id] = Live(server_id)
    return lv


def peek(server_id: str) -> Live | None:
    return _live.get(server_id)


def forget(server_id: str) -> None:
    _live.pop(server_id, None)


def all_live() -> dict[str, Live]:
    return _live


def point_of(s: dict) -> tuple:
    disks = s.get("disks") or []
    return (
        s["cpu"]["pct"], s["cpu"]["iowait"], s["cpu"]["steal"],
        s["mem"]["pct"], s["mem"]["swap_pct"], s["load"][0],
        max((d["pct"] for d in disks), default=0.0),
        s["net"]["rx_bps"], s["net"]["tx_bps"], s["io"]["read_bps"], s["io"]["write_bps"],
        float(s["tcp"]),
    )


def ingest(lv: Live, raw: dict, now: float | None = None) -> dict:
    """Merge one sample into live state; returns the merged 'last' snapshot."""
    now = now or time.time()
    s = clean_sample(raw)
    ts = s["ts"] / 1000 if s["ts"] else now
    if ts > now + 60 or ts < now - 900:  # agent clock is off: trust ours
        ts = now
    s["ts"] = int(ts * 1000)
    # Slow parts (disks, processes, containers) arrive together every few
    # samples. Between refreshes keep the previous ones; on a refresh take
    # only what came (a vanished Docker must not leave stale containers).
    if "disks" not in raw and "top" not in raw:
        for k in ("disks", "top", "containers", "docker_err"):
            if k in lv.last:
                s[k] = lv.last[k]
    lv.last = s
    lv.last_ts = now
    p = point_of(s)
    lv.ring.append((int(ts),) + p)
    _accumulate(lv, int(ts), p)
    if now - lv.seen_written > 60:
        db.x("UPDATE servers SET last_seen=? WHERE id=?", (int(now), lv.server_id))
        lv.seen_written = now
    return s


def _accumulate(lv: Live, ts: int, p: tuple) -> None:
    minute = ts - ts % 60
    if lv.acc_n and minute != lv.acc_minute:
        flush(lv)
    if not lv.acc_n:
        lv.acc_minute = minute
    for i, v in enumerate(p):
        lv.acc[i] += v
    lv.acc_cpu_max = max(lv.acc_cpu_max, p[0])
    lv.acc_n += 1


def flush(lv: Live) -> None:
    """Write the minute. If the row exists (agent reconnected or sent its
    buffer mid-minute), merge as a weighted average instead of overwriting."""
    if not lv.acc_n:
        return
    avg = [round(v / lv.acc_n, 2) for v in lv.acc]
    cols = ", ".join(FIELDS)
    merge = ", ".join(f"{c}=ROUND(({c}*n + excluded.{c}*excluded.n)/(n + excluded.n), 2)" for c in FIELDS)
    db.x(f"INSERT INTO samples_1m(server_id, ts, n, cpu_max, {cols}) "
         f"VALUES(?,?,?,?,{','.join('?' * len(FIELDS))}) "
         f"ON CONFLICT(server_id, ts) DO UPDATE SET {merge}, "
         f"cpu_max=MAX(cpu_max, excluded.cpu_max), n=n + excluded.n",
         (lv.server_id, lv.acc_minute, lv.acc_n, round(lv.acc_cpu_max, 2), *avg))
    lv.acc = [0.0] * len(FIELDS)
    lv.acc_cpu_max = 0.0
    lv.acc_n = 0


def flush_all() -> None:
    for lv in _live.values():
        flush(lv)
