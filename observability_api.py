"""API de observabilidad en tiempo real (FastAPI + WebSocket)."""

from __future__ import annotations

import asyncio
from fastapi import FastAPI, WebSocket
from telemetry import TelemetryBus

app = FastAPI(title="Agent Observability")
telemetry = TelemetryBus()


@app.get('/healthz')
def healthz():
    return {"ok": True}


@app.get('/snapshot')
def snapshot():
    return telemetry.snapshot()


@app.websocket('/ws/live')
async def ws_live(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json(telemetry.snapshot())
            await asyncio.sleep(1)
    finally:
        await ws.close()
