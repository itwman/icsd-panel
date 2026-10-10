"""Ask an agent to do something and wait for its answer.

Requests and answers travel over the agent's existing WebSocket, matched by
id. The agent enforces its own policy; the hub only relays and records.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time

from icsd_hub import live


class RPCError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def call(sid: str, op: str, args: dict | None = None, timeout: float = 60) -> object:
    lv = live.peek(sid)
    ws = lv.ws if lv else None
    if ws is None:
        raise RPCError("این سرور الان به پنل وصل نیست", 503)
    if (lv.caps or {}).get("mode") != "manage":
        raise RPCError("این سرور در حالت «فقط مانیتورینگ» است؛ برای مدیریت، agent را با --upgrade نصب کنید", 409)
    rid = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    lv.pending[rid] = fut
    try:
        if lv.send_lock is None:
            lv.send_lock = asyncio.Lock()
        try:
            async with lv.send_lock:
                await ws.send_text(json.dumps({"t": "req", "id": rid, "op": op, "args": args or {}}))
        except Exception:  # noqa: BLE001 — the socket closed under us
            raise RPCError("ارتباط با agent قطع شد", 503) from None
        msg = await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        raise RPCError("agent در زمان مقرر پاسخ نداد", 504) from None
    finally:
        lv.pending.pop(rid, None)
    if msg.get("error"):
        raise RPCError(str(msg["error"])[:1000], 400)
    return msg.get("d")


async def wait_job(sid: str, job_id: str, timeout: float = 6 * 3600, every: float = 3,
                   on_log=None) -> dict:
    """Poll a background job on the agent until it ends."""
    end = time.time() + timeout
    misses = 0
    while time.time() < end:
        try:
            j = await call(sid, "job.status", {"id": job_id}, timeout=30)
            misses = 0
        except RPCError as e:
            if e.status in (503, 504) and misses < 40:   # agent reconnecting: be patient (~2 min)
                misses += 1
                await asyncio.sleep(every)
                continue
            raise
        if on_log and j.get("log"):
            on_log(j["log"])
        if j.get("state") != "running":
            return j
        await asyncio.sleep(every)
    raise RPCError("کار بیش از حد طول کشید", 504)
