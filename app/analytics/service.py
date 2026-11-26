"""Assembles the payloads for the overview dashboard and the performance report."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from app.analytics.efficiency import cluster_totals, health_score, per_service
from app.analytics.incidents import compute_incident_kpis, incident_summary
from app.core.store import store
from app.metrics.insights import summarize
from app.metrics.models import Insight, PodSnapshot
from app.metrics.service import MetricsError, metrics_service


def incident_block(limit_recent: int = 6) -> Dict[str, Any]:
    now = time.time()
    incidents = store.list()
    created = {i["incident_id"]: store.created_at(i["incident_id"]) or now for i in incidents}
    return {
        "kpis": compute_incident_kpis(incidents, created, now),
        "recent": [incident_summary(i, created[i["incident_id"]], now) for i in incidents[:limit_recent]],
    }


async def cluster_block(namespace: str, range_s: int = 1800) -> Dict[str, Any]:
    """Cluster-side analytics for one namespace. Never raises: errors become `error`."""
    try:
        dash = await metrics_service.dashboard(namespace, None, range_s)
    except MetricsError as e:
        return {"namespace": namespace, "error": str(e)}
    pods = [PodSnapshot(**p) for p in dash["pods"]]
    insights = [Insight(**i) for i in dash["insights"]]
    return {
        "namespace": namespace,
        "error": None,
        "synthetic": dash["source"]["synthetic"],
        "series_source": dash["source"]["series_source"],
        "summary": summarize(pods),
        "health": health_score(pods, insights),
        "totals": cluster_totals(pods),
        "services": per_service(pods),
        "insights": [i.model_dump() for i in insights],
    }


async def build_overview(namespace: str) -> Dict[str, Any]:
    inc = incident_block()
    cluster = await cluster_block(namespace)
    return {
        "generated_at": time.time(),
        "namespace": namespace,
        "incidents": inc,
        "cluster": cluster,
    }


async def build_performance(namespace: str) -> Dict[str, Any]:
    inc = incident_block(limit_recent=10)
    cluster = await cluster_block(namespace)
    return {"generated_at": time.time(), "namespace": namespace, "incidents": inc, "cluster": cluster}
