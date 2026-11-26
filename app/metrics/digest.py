"""Compact, token-friendly views of dashboard data for the AI assistant."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.metrics.insights import linear_trend, recent_segment

MIB = 2**20


def _stats(vals: List[Optional[float]]) -> Optional[Dict[str, float]]:
    pts = [v for v in vals if v is not None]
    if not pts:
        return None
    return {"last": pts[-1], "avg": sum(pts) / len(pts), "max": max(pts), "min": min(pts)}


def _round(x: Optional[float], nd: int = 1) -> Optional[float]:
    return None if x is None else round(x, nd)


def series_digest(series: Optional[dict], pods: List[str]) -> Dict[str, Any]:
    """Per-pod last/avg/max (+ memory trend) instead of hundreds of raw points."""
    if not series or not series.get("timestamps"):
        return {}
    ts = series["timestamps"]
    out: Dict[str, Any] = {}
    for pod in pods:
        row: Dict[str, Any] = {}
        for metric, scale, nd in (("cpu_m", 1, 1), ("mem_b", 1 / MIB, 1), ("net_rx_bps", 1 / 1024, 1),
                                  ("net_tx_bps", 1 / 1024, 1), ("cpu_throttle_pct", 1, 1)):
            vals = (series["metrics"].get(metric) or {}).get(pod)
            st = _stats(vals) if vals else None
            if st:
                unit = {"cpu_m": "cpu_millicores", "mem_b": "mem_mib", "net_rx_bps": "net_rx_kib_s",
                        "net_tx_bps": "net_tx_kib_s", "cpu_throttle_pct": "cpu_throttle_pct"}[metric]
                row[unit] = {k: _round(v * scale, nd) for k, v in st.items()}
        mem = (series["metrics"].get("mem_b") or {}).get(pod)
        if mem:
            seg_t, seg_v = recent_segment(ts, mem, (series["metrics"].get("restarts") or {}).get(pod))
            tr = linear_trend(seg_t, seg_v)
            if tr:
                row["mem_trend_mib_per_min"] = _round(tr["slope"] * 60 / MIB, 2)
                row["mem_trend_r2"] = _round(tr["r2"], 2)
        rs = [v for v in (series["metrics"].get("restarts") or {}).get(pod, []) if v is not None]
        if len(rs) >= 2:
            row["restarts_in_window"] = int(rs[-1] - rs[0])
        if row:
            out[pod] = row
    return out


def dashboard_digest(dash: Dict[str, Any], max_pods: int = 25) -> Dict[str, Any]:
    pods = dash["pods"]
    # unhealthy / busiest first so truncation drops the boring ones
    def rank(p: Dict[str, Any]) -> tuple:
        bad = 0 if (p["phase"] != "Running" or not p["ready"] or p["restart_count"]) else 1
        return (bad, -(p.get("cpu_m") or 0))

    chosen = sorted(pods, key=rank)[:max_pods]
    names = [p["name"] for p in chosen]
    return {
        "namespace": dash["namespace"],
        "service": dash["service"],
        "range_seconds": dash["range_seconds"],
        "data_is_synthetic_demo": dash["source"]["synthetic"],
        "series_source": dash["source"]["series_source"],
        "history_available_seconds": dash["history"]["available_seconds"],
        "summary": {k: _round(v, 1) if isinstance(v, float) else v for k, v in dash["summary"].items()},
        "pods": [
            {
                "name": p["name"], "service": p["service"], "phase": p["phase"], "ready": p["ready"],
                "restarts": p["restart_count"], "reason": p["reason"],
                "last_termination": p["last_termination_reason"],
                "cpu_m": _round(p["cpu_m"]), "cpu_limit_m": _round(p["cpu_lim_m"]),
                "mem_mib": _round(p["mem_b"] / MIB) if p["mem_b"] is not None else None,
                "mem_limit_mib": _round(p["mem_lim_b"] / MIB) if p["mem_lim_b"] else None,
            }
            for p in chosen
        ],
        "pods_omitted": max(0, len(pods) - len(chosen)),
        "series_stats_per_pod": series_digest(dash.get("series"), names),
        "insights": dash["insights"],
    }
