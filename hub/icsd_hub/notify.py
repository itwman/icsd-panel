"""Alerts to Telegram and Bale (the same Bot API, different host).

Bale is reachable from inside Iran; Telegram usually is not. If the hub runs
in Iran and you want Telegram, set a Bot-API mirror/proxy as `telegram_api`.

اعلان به تلگرام و بله. بله از داخل ایران در دسترس است.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from icsd_hub import db

log = logging.getLogger("icsd.notify")

DEFAULTS = {
    "telegram_on": False, "telegram_token": "", "telegram_chat": "",
    "telegram_api": "https://api.telegram.org",
    "bale_on": False, "bale_token": "", "bale_chat": "",
    "bale_api": "https://tapi.bale.ai",
}
SECRET_KEYS = ("telegram_token", "bale_token")
TEHRAN = timezone(timedelta(hours=3, minutes=30))  # Iran has no DST since 2022


def load() -> dict:
    return {**DEFAULTS, **(db.get_setting("notify", {}) or {})}


def public_view() -> dict:
    """Settings for the UI: tokens are never sent back, only whether set."""
    cfg = load()
    for k in SECRET_KEYS:
        cfg[k + "_set"] = bool(cfg[k])
        cfg[k] = ""
    return cfg


def save(update: dict) -> dict:
    cfg = load()
    for ch in ("telegram", "bale"):
        new_api = str(update.get(f"{ch}_api") or cfg[f"{ch}_api"]).strip().rstrip("/")
        if new_api != cfg[f"{ch}_api"] and cfg[f"{ch}_token"] and not update.get(f"{ch}_token"):
            # Otherwise an admin session could redirect the stored token to any host.
            raise ValueError("برای تغییر آدرس API، توکن ربات را هم دوباره وارد کنید")
    for k, default in DEFAULTS.items():
        if k not in update:
            continue
        v = update[k]
        if k in SECRET_KEYS and not v:
            continue  # empty field in the form = keep the stored token
        cfg[k] = bool(v) if isinstance(default, bool) else str(v).strip()[:300]
    for k in ("telegram_api", "bale_api"):
        if not cfg[k].startswith("https://"):
            cfg[k] = DEFAULTS[k]
        cfg[k] = cfg[k].rstrip("/")
    db.set_setting("notify", cfg)
    return public_view()


def jalali(ts: float | None = None) -> str:
    """'۱۴۰۵/۰۷/۱۸ ۱۴:۰۵' in Tehran time — no third-party calendar library."""
    d = datetime.fromtimestamp(ts or time.time(), TEHRAN)
    gy, gm, gd = d.year, d.month, d.day
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    jm = 1 + days // 31 if days < 186 else 7 + (days - 186) // 30
    jd = 1 + (days % 31 if days < 186 else (days - 186) % 30)
    s = f"{jy}/{jm:02d}/{jd:02d} {d.hour:02d}:{d.minute:02d}"
    return s.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


async def _bot(client: httpx.AsyncClient, api: str, token: str, chat: str, text: str) -> None:
    r = await client.post(f"{api}/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not body.get("ok", False):
        raise RuntimeError(body.get("description") or f"HTTP {r.status_code}")


async def send(text: str, cfg: dict | None = None) -> list[dict]:
    """Send to every enabled channel; never raises. Returns per-channel results."""
    cfg = cfg or load()
    jobs = []
    if cfg["telegram_on"] and cfg["telegram_token"] and cfg["telegram_chat"]:
        jobs.append(("telegram", cfg["telegram_api"], cfg["telegram_token"], cfg["telegram_chat"]))
    if cfg["bale_on"] and cfg["bale_token"] and cfg["bale_chat"]:
        jobs.append(("bale", cfg["bale_api"], cfg["bale_token"], cfg["bale_chat"]))
    results = []
    if not jobs:
        return results
    async with httpx.AsyncClient(timeout=15) as client:
        outcomes = await asyncio.gather(
            *(_bot(client, api, token, chat, text) for _, api, token, chat in jobs),
            return_exceptions=True)
    for (name, *_), res in zip(jobs, outcomes):
        err = "" if res is None else str(res)
        # Never log the token (it is part of the URL in httpx errors).
        for k in SECRET_KEYS:
            if cfg[k]:
                err = err.replace(cfg[k], "***")
        db.x("INSERT INTO notify_log(ts, channel, ok, error) VALUES(?,?,?,?)",
             (db.now(), name, int(not err), err[:300]))
        if err:
            log.warning("notify %s failed: %s", name, err)
        results.append({"channel": name, "ok": not err, "error": err})
    return results


_tasks: set[asyncio.Task] = set()


def fire(text: str) -> None:
    """Send in the background (alert evaluation must not wait on the network)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(send(text))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
