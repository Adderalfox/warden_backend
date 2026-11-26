"""Infrastructure efficiency + a transparent cluster health score, from pod snapshots/insights."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.metrics.models import Insight, PodSnapshot


def _pct(num: Optional[float], den: Optional[float]) -> Optional[float]:
    return None if num is None or not den else 100.0 * num / den


def per_service(pods: Sequence[PodSnapshot]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[PodSnapshot]] = {}
    for p in pods:
        groups.setdefault(p.service, []).append(p)
    rows = []
    for svc, ps in sorted(groups.items()):
        def tot(attr: str) -> Optional[float]:
            vals = [getattr(p, attr) for p in ps if getattr(p, attr) is not None]
            return float(sum(vals)) if vals else None

        cpu, mem = tot("cpu_m"), tot("mem_b")
        cpu_req, mem_req = tot("cpu_req_m"), tot("mem_req_b")
        cpu_lim, mem_lim = tot("cpu_lim_m"), tot("mem_lim_b")
        cpu_util, mem_util = _pct(cpu, cpu_req), _pct(mem, mem_req)
        flags = []
        if cpu_util is not None and cpu_util < 30 and cpu_req and cpu_req >= 100:
            flags.append("cpu_overprovisioned")
        if mem_util is not None and mem_util < 30 and mem_req and mem_req >= 128 * 2**20:
            flags.append("memory_overprovisioned")
        if _pct(mem, mem_lim) is not None and _pct(mem, mem_lim) >= 85:
            flags.append("memory_near_limit")
        if _pct(cpu, cpu_lim) is not None and _pct(cpu, cpu_lim) >= 85:
            flags.append("cpu_near_limit")
        ready = sum(1 for p in ps if p.ready)
        rows.append({
            "service": svc,
            "pods": len(ps),
            "ready": ready,
            "availability_pct": 100.0 * ready / len(ps),
            "restarts": sum(p.restart_count for p in ps),
            "cpu_m": cpu, "cpu_req_m": cpu_req, "cpu_lim_m": cpu_lim,
            "mem_b": mem, "mem_req_b": mem_req, "mem_lim_b": mem_lim,
            "cpu_util_of_request_pct": cpu_util,
            "mem_util_of_request_pct": mem_util,
            "cpu_pct_of_limit": _pct(cpu, cpu_lim),
            "mem_pct_of_limit": _pct(mem, mem_lim),
            "cpu_headroom_m": (cpu_req - cpu) if cpu is not None and cpu_req else None,
            "mem_headroom_b": (mem_req - mem) if mem is not None and mem_req else None,
            "flags": flags,
        })
    return rows


def health_score(pods: Sequence[PodSnapshot], insights: Sequence[Insight]) -> Dict[str, Any]:
    """100 minus capped penalties. The breakdown is returned so the UI can explain the number."""
    if not pods:
        return {"score": None, "grade": "unknown", "penalties": []}
    crit = sum(1 for i in insights if i.severity == "critical")
    warn = sum(1 for i in insights if i.severity == "warning")
    unhealthy = sum(1 for p in pods if p.phase in ("Pending", "Failed") or (p.phase == "Running" and not p.ready))
    penalties = [
        {"label": f"{crit} critical issue(s)", "points": min(60, 15 * crit)},
        {"label": f"{warn} warning(s)", "points": min(25, 4 * warn)},
        {"label": f"{unhealthy}/{len(pods)} pod(s) not serving", "points": round(min(15, 15 * unhealthy / len(pods)))},
    ]
    score = max(0, 100 - sum(p["points"] for p in penalties))
    grade = "healthy" if score >= 85 else "degraded" if score >= 60 else "critical"
    return {"score": score, "grade": grade, "penalties": [p for p in penalties if p["points"]]}


def cluster_totals(pods: Sequence[PodSnapshot]) -> Dict[str, Any]:
    rows = per_service(pods)
    cpu_head = sum(r["cpu_headroom_m"] for r in rows if r["cpu_headroom_m"] and r["cpu_headroom_m"] > 0)
    mem_head = sum(r["mem_headroom_b"] for r in rows if r["mem_headroom_b"] and r["mem_headroom_b"] > 0)
    ready = sum(1 for p in pods if p.ready)

    def tot(attr: str) -> Optional[float]:
        vals = [getattr(p, attr) for p in pods if getattr(p, attr) is not None]
        return float(sum(vals)) if vals else None

    cpu, cpu_req = tot("cpu_m"), tot("cpu_req_m")
    mem, mem_req = tot("mem_b"), tot("mem_req_b")
    return {
        "availability_pct": (100.0 * ready / len(pods)) if pods else None,
        "cpu_util_of_request_pct": _pct(cpu, cpu_req),
        "mem_util_of_request_pct": _pct(mem, mem_req),
        "cpu_unused_request_m": cpu_head,
        "mem_unused_request_b": mem_head,
        "restarts_total": sum(p.restart_count for p in pods),
        "overprovisioned_services": [r["service"] for r in rows if any(f.endswith("overprovisioned") for f in r["flags"])],
    }
