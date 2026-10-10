"""Monitoring endpoints — REST snapshot + live WebSocket stream.

نقاط پایانی مانیتورینگ: یک snapshot از طریق REST و استریم زندهٔ WebSocket.
"""
from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.services import metrics

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


@router.get("/snapshot")
async def get_snapshot() -> dict:
    """A single point-in-time metrics snapshot."""
    return metrics.snapshot()


@router.get("/history")
async def get_history(hours: int = 24) -> dict:
    """Historical metrics time-series for charts (default last 24h)."""
    return metrics.history(hours=hours)


@router.websocket("/ws")
async def metrics_ws(ws: WebSocket) -> None:
    """Live metrics stream — pushes a snapshot every metrics_interval_seconds."""
    await ws.accept()
    try:
        while True:
            await ws.send_json(metrics.snapshot())
            await asyncio.sleep(settings.metrics_interval_seconds)
    except WebSocketDisconnect:
        return
    except Exception:
        with contextlib.suppress(Exception):
            await ws.close()
