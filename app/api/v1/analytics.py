from __future__ import annotations

from fastapi import APIRouter, Query

from app.analytics.service import build_overview, build_performance

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


@router.get("/overview")
async def overview(namespace: str = Query("default")):
    return await build_overview(namespace)


@router.get("/performance")
async def performance(namespace: str = Query("default")):
    return await build_performance(namespace)
