"""Platform app: blind relay (M2); accounts + registry land in M3.

Serve with WS keepalive so dead legs are reaped (plan §heartbeats):
    uvicorn app.main:app --ws-ping-interval 20 --ws-ping-timeout 25
"""

from __future__ import annotations

from fastapi import FastAPI

from .relay import router as relay_router

app = FastAPI(title="bcrc-platform")
app.include_router(relay_router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
