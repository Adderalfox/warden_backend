"""Pod analytics API consumed by the Grafana-style dashboard."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.metrics.service import MetricsError, metrics_service, parse_range

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


def _range(value: str) -> int:
    try:
        return parse_range(value)
    except MetricsError as e:
        raise HTTPException(422, str(e)) from e


def _unavailable(e: MetricsError) -> HTTPException:
    return HTTPException(503, str(e))


@router.get("/status")
async def status():
    return await metrics_service.status()


@router.get("/namespaces")
async def namespaces():
    try:
        return {"namespaces": await metrics_service.namespaces()}
    except MetricsError as e:
        raise _unavailable(e) from e


@router.get("/services")
async def services(namespace: str = Query("default")):
    try:
        return {"namespace": namespace, "services": await metrics_service.services(namespace)}
    except MetricsError as e:
        raise _unavailable(e) from e


@router.get("/dashboard")
async def dashboard(
    namespace: str = Query("default"),
    service: Optional[str] = Query(None),
    range_: str = Query("1h", alias="range"),
):
    seconds = _range(range_)
    try:
        return await metrics_service.dashboard(namespace, service or None, seconds)
    except MetricsError as e:
        raise _unavailable(e) from e


@router.get("/pods/{namespace}/{pod}")
async def pod_detail(namespace: str, pod: str, range_: str = Query("1h", alias="range")):
    seconds = _range(range_)
    try:
        return await metrics_service.pod_detail(namespace, pod, seconds)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except MetricsError as e:
        raise _unavailable(e) from e


@router.get("/pods/{namespace}/{pod}/events")
async def pod_events(namespace: str, pod: str):
    try:
        return {"events": await metrics_service.events(namespace, pod)}
    except MetricsError as e:
        raise _unavailable(e) from e


@router.get("/pods/{namespace}/{pod}/logs", response_class=PlainTextResponse)
async def pod_logs(
    namespace: str,
    pod: str,
    container: Optional[str] = None,
    lines: int = Query(200, ge=1, le=2000),
    previous: bool = False,
):
    try:
        return await metrics_service.logs(namespace, pod, container, lines, previous)
    except MetricsError as e:
        raise _unavailable(e) from e
