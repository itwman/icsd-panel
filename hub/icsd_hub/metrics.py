"""History: which table answers which time range, rollup and retention.

    ≤ 1 hour   → the in-memory ring (every sample), if the hub has it
    ≤ 2 days   → samples_1m
    longer     → samples_1h  (kept ~13 months)

Charts get columns ({"t": [...], "cpu": [...]}) — the shape uPlot wants,
and much smaller than a list of objects.
"""
from __future__ import annotations

import time

from icsd_hub import config, db, live

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400,
          "30d": 30 * 86400, "90d": 90 * 86400, "1y": 365 * 86400}
COLS = ("cpu_max",) + live.FIELDS


def series(server_id: str, rng: str) -> dict:
    span = RANGES.get(rng, 3600)
    now = int(time.time())
    since = now - span
    lv = live.peek(server_id)
    if span <= 3600 and lv and lv.ring:
        # Recent part from RAM (every sample); anything older — e.g. the hub
        # restarted 10 minutes ago — from the per-minute table.
        start = lv.ring[0][0]
        older = db.q(f"SELECT ts, {', '.join(live.FIELDS)} FROM samples_1m "
                     f"WHERE server_id=? AND ts>=? AND ts<? ORDER BY ts",
                     (server_id, since, start - start % 60)) if start > since else []
        pts = [(r["ts"], *(r[c] for c in live.FIELDS)) for r in older]
        pts += [p for p in lv.ring if p[0] >= since]
        out: dict = {"step": config.settings.sample_interval, "t": [p[0] for p in pts]}
        for i, name in enumerate(live.FIELDS, start=1):
            out[name] = [p[i] for p in pts]
        out["cpu_max"] = out["cpu"]
        return out
    table, step = ("samples_1m", 60) if span <= 2 * 86400 else ("samples_1h", 3600)
    rows = db.q(f"SELECT ts, {', '.join(COLS)} FROM {table} "
                f"WHERE server_id=? AND ts>=? ORDER BY ts", (server_id, since))
    if table == "samples_1h" and span <= 30 * 86400:
        # The current hour is not rolled up yet: append it from minutes.
        last = rows[-1]["ts"] if rows else since
        rows += _hourly_from_minutes(server_id, last + 3600)
    out = {"step": step, "t": [r["ts"] for r in rows]}
    for c in COLS:
        out[c] = [r[c] for r in rows]
    return out


def _hourly_from_minutes(server_id: str, since: int) -> list[dict]:
    avg = ", ".join(f"ROUND(AVG({c}), 2) AS {c}" for c in live.FIELDS)
    return db.q(f"SELECT (ts/3600)*3600 AS ts, MAX(cpu_max) AS cpu_max, {avg} "
                f"FROM samples_1m WHERE server_id=? AND ts>=? GROUP BY ts/3600 ORDER BY 1",
                (server_id, since))


def rollup(now: int | None = None) -> None:
    """Hourly averages for the last few complete hours (idempotent)."""
    now = now or int(time.time())
    this_hour = now - now % 3600
    avg = ", ".join(f"ROUND(AVG({c}), 2)" for c in live.FIELDS)
    db.x(f"INSERT OR REPLACE INTO samples_1h(server_id, ts, cpu_max, {', '.join(live.FIELDS)}) "
         f"SELECT server_id, (ts/3600)*3600, MAX(cpu_max), {avg} FROM samples_1m "
         f"WHERE ts>=? AND ts<? GROUP BY server_id, ts/3600",
         (this_hour - 6 * 3600, this_hour))


def prune(now: int | None = None) -> None:
    now = now or int(time.time())
    s = config.settings
    db.x("DELETE FROM samples_1m WHERE ts<?", (now - s.keep_minutes_days * 86400,))
    db.x("DELETE FROM samples_1h WHERE ts<?", (now - s.keep_hours_days * 86400,))
    db.x("DELETE FROM check_log WHERE ts<?", (now - 30 * 86400,))
    db.x("DELETE FROM events WHERE ended IS NOT NULL AND ended<?", (now - s.keep_events_days * 86400,))
    db.x("DELETE FROM audit WHERE ts<?", (now - 365 * 86400,))
    db.x("DELETE FROM notify_log WHERE ts<?", (now - 30 * 86400,))


def sparkline(server_id: str, points: int = 40) -> list[float]:
    """CPU over the last ~30 minutes for the overview cards."""
    lv = live.peek(server_id)
    if lv and len(lv.ring) >= 4:
        data = [p[1] for p in list(lv.ring)[-360:]]
    else:
        rows = db.q("SELECT cpu FROM samples_1m WHERE server_id=? AND ts>=? ORDER BY ts",
                    (server_id, int(time.time()) - 1800))
        data = [r["cpu"] for r in rows]
    if len(data) <= points:
        return [round(v, 1) for v in data]
    size = len(data) / points
    return [round(max(data[int(i * size):int((i + 1) * size)] or [0]), 1) for i in range(points)]
