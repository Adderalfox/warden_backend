"""
Deterministic health rules over pod snapshots + recent series. These are the
"analytics" on top of raw graphs: OOM kills, crash loops, memory-leak ETA,
CPU limit pressure, restarts in the selected window, readiness, missing limits.
"""
from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Sequence

from app.metrics.models import Insight, PodSnapshot, SeriesResult

BAD_WAITING = {
    "CrashLoopBackOff": "critical",
    "ImagePullBackOff": "critical",
    "ErrImagePull": "critical",
    "CreateContainerConfigError": "critical",
    "CreateContainerError": "critical",
    "InvalidImageName": "critical",
    "RunContainerError": "critical",
}
SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _fmt_bytes(b: float) -> str:
    for unit, div in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if b >= div:
            return f"{b / div:.1f} {unit}"
    return f"{b:.0f} B"


def _fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def linear_trend(ts: Sequence[float], ys: Sequence[Optional[float]]) -> Optional[Dict[str, float]]:
    """Least-squares fit over non-null points. Returns slope (per second), r2, last."""
    pts = [(t, y) for t, y in zip(ts, ys) if y is not None]
    if len(pts) < 6 or pts[-1][0] - pts[0][0] < 180:
        return None
    n = len(pts)
    mx = sum(t for t, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((t - mx) ** 2 for t, _ in pts)
    if sxx == 0:
        return None
    sxy = sum((t - mx) * (y - my) for t, y in pts)
    slope = sxy / sxx
    ss_tot = sum((y - my) ** 2 for _, y in pts)
    ss_res = sum((y - (my + slope * (t - mx))) ** 2 for t, y in pts)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"slope": slope, "r2": r2, "last": pts[-1][1]}


def compute_insights(
    pods: Sequence[PodSnapshot],
    series: Optional[SeriesResult] = None,
    now: Optional[float] = None,
    metrics_warning: Optional[str] = None,
) -> List[Insight]:
    now = now or time.time()
    out: List[Insight] = []

    if not pods:
        return out
    if metrics_warning:
        out.append(Insight(severity="info", kind="no_metrics", title="No CPU/memory usage data", detail=metrics_warning))

    for p in pods:
        # --- container waiting states ---
        for c in p.containers:
            if c.state == "waiting" and c.reason in BAD_WAITING:
                extra = ""
                if c.last_termination_reason:
                    extra = f" Last exit: {c.last_termination_reason}" + (
                        f" (code {c.last_termination_exit_code})" if c.last_termination_exit_code is not None else ""
                    ) + "."
                out.append(Insight(
                    severity=BAD_WAITING[c.reason], kind=c.reason.lower(), pod=p.name,
                    title=f"{c.reason} on {p.name}",
                    detail=f"Container '{c.name}' is waiting: {c.message or c.reason}.{extra} Restarts: {c.restart_count}.",
                ))

        # --- OOM kills ---
        if p.last_termination_reason == "OOMKilled":
            age = (now - p.last_termination_at) if p.last_termination_at else None
            if age is None or age < 24 * 3600:
                lim = f" (memory limit {_fmt_bytes(p.mem_lim_b)})" if p.mem_lim_b else ""
                out.append(Insight(
                    severity="critical", kind="oom_killed", pod=p.name,
                    title=f"{p.name} was OOMKilled",
                    detail=f"Container killed for exceeding its memory limit{lim}"
                           + (f" {_fmt_dur(age)} ago." if age is not None else ".")
                           + " Raise the limit or fix the leak.",
                ))

        # --- memory / cpu pressure vs limits ---
        if p.mem_b is not None and p.mem_lim_b:
            pct = 100 * p.mem_b / p.mem_lim_b
            if pct >= 90:
                out.append(Insight(severity="critical", kind="memory_pressure", pod=p.name,
                                   title=f"{p.name} memory at {pct:.0f}% of limit",
                                   detail=f"{_fmt_bytes(p.mem_b)} used of {_fmt_bytes(p.mem_lim_b)}. OOM kill is imminent."))
            elif pct >= 75:
                out.append(Insight(severity="warning", kind="memory_pressure", pod=p.name,
                                   title=f"{p.name} memory at {pct:.0f}% of limit",
                                   detail=f"{_fmt_bytes(p.mem_b)} used of {_fmt_bytes(p.mem_lim_b)}."))
        if p.cpu_m is not None and p.cpu_lim_m:
            pct = 100 * p.cpu_m / p.cpu_lim_m
            if pct >= 90:
                out.append(Insight(severity="warning", kind="cpu_pressure", pod=p.name,
                                   title=f"{p.name} CPU at {pct:.0f}% of limit",
                                   detail=f"{p.cpu_m:.0f}m used of {p.cpu_lim_m:.0f}m limit - the container is likely being CPU-throttled."))

        # --- readiness / scheduling ---
        if p.phase == "Running" and not p.ready and not any(c.reason in BAD_WAITING for c in p.containers):
            out.append(Insight(severity="warning", kind="not_ready", pod=p.name,
                               title=f"{p.name} is Running but not Ready ({p.ready_containers})",
                               detail="Readiness probe failing or containers still starting."))
        if p.phase == "Pending" and p.created_at and now - p.created_at > 300:
            out.append(Insight(severity="warning", kind="pending", pod=p.name,
                               title=f"{p.name} Pending for {_fmt_dur(now - p.created_at)}",
                               detail=p.message or p.reason or "Pod has not been scheduled (check node capacity / taints / PVCs)."))
        if p.phase == "Failed":
            out.append(Insight(severity="critical", kind="failed", pod=p.name,
                               title=f"{p.name} is in Failed phase", detail=p.message or p.reason or "Pod terminated unsuccessfully."))

        # --- resource hygiene ---
        if p.phase == "Running" and p.mem_lim_b is None:
            out.append(Insight(severity="info", kind="no_memory_limit", pod=p.name,
                               title=f"{p.name} has no memory limit",
                               detail="Without a limit this pod can consume node memory and trigger evictions."))

    # --- series-based rules ---
    if series and series.timestamps:
        by_name = {p.name: p for p in pods}
        for pod, mem in series.metrics.get("mem_b", {}).items():
            p = by_name.get(pod)
            if not p or not p.mem_lim_b:
                continue
            tail = [(t, v) for t, v in zip(series.timestamps, mem) if t >= series.timestamps[-1] - 1800]
            trend = linear_trend([t for t, _ in tail], [v for _, v in tail])
            if trend and trend["slope"] > 0 and trend["r2"] >= 0.6 and trend["last"] < p.mem_lim_b:
                eta = (p.mem_lim_b - trend["last"]) / trend["slope"]
                if eta < 3600:
                    per_min = trend["slope"] * 60
                    out.append(Insight(
                        severity="critical" if eta < 900 else "warning", kind="memory_leak", pod=pod,
                        title=f"{pod} memory growing, limit in ~{_fmt_dur(eta)}",
                        detail=f"Steady growth of ~{_fmt_bytes(per_min)}/min (fit r²={trend['r2']:.2f}); at this rate it hits "
                               f"the {_fmt_bytes(p.mem_lim_b)} limit in about {_fmt_dur(eta)}. Likely a memory leak.",
                    ))
        for pod, rs in series.metrics.get("restarts", {}).items():
            vals = [v for v in rs if v is not None]
            if len(vals) >= 2 and vals[-1] > vals[0]:
                n = int(vals[-1] - vals[0])
                out.append(Insight(severity="warning", kind="restarts_in_window", pod=pod,
                                   title=f"{pod} restarted {n}x in the selected window",
                                   detail="Container restarts during this time range; check logs of the previous container and events."))

    out.sort(key=lambda i: (SEV_ORDER.get(i.severity, 3), i.pod or ""))
    return out


def summarize(pods: Sequence[PodSnapshot], series: Optional[SeriesResult] = None) -> Dict[str, Optional[float]]:
    def tot(attr: str) -> Optional[float]:
        vals = [getattr(p, attr) for p in pods if getattr(p, attr) is not None]
        return float(sum(vals)) if vals else None

    restarts_window = 0
    if series:
        for rs in series.metrics.get("restarts", {}).values():
            vals = [v for v in rs if v is not None]
            if len(vals) >= 2 and vals[-1] > vals[0]:
                restarts_window += int(vals[-1] - vals[0])

    cpu_lims = [p.cpu_lim_m for p in pods if p.cpu_lim_m]
    mem_lims = [p.mem_lim_b for p in pods if p.mem_lim_b]
    return {
        "pods_total": len(pods),
        "running": sum(1 for p in pods if p.phase == "Running"),
        "pending": sum(1 for p in pods if p.phase == "Pending"),
        "failed": sum(1 for p in pods if p.phase == "Failed"),
        "not_ready": sum(1 for p in pods if not p.ready and p.phase == "Running"),
        "restarts_total": sum(p.restart_count for p in pods),
        "restarts_in_window": restarts_window,
        "cpu_m_total": tot("cpu_m"),
        "mem_b_total": tot("mem_b"),
        "cpu_req_m_total": tot("cpu_req_m"),
        "cpu_lim_m_total": float(sum(cpu_lims)) if cpu_lims else None,
        "mem_req_b_total": tot("mem_req_b"),
        "mem_lim_b_total": float(sum(mem_lims)) if mem_lims else None,
    }
