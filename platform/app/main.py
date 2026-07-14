"""Platform app stub — grows into accounts + registry + blind relay (M2+)."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="bcrc-platform")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
