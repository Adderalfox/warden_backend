"""
Synthetic demo cluster -- ONLY used when ``WARDEN_METRICS_SOURCE=demo``.

Lets you explore the analytics UI without a cluster. Everything it returns is
flagged ``synthetic: true`` by the API and the UI shows a permanent
"DEMO DATA" banner. Values are deterministic functions of wall-clock time, so
history for any time range is available immediately and charts stay stable
across refreshes. The default source is real Kubernetes; this is opt-in.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from app.metrics.models import ContainerSnapshot, PodSnapshot, SeriesResult
from app.metrics.store import grid

NAMESPACE = "demo"
MIB = 2**20
T0 = 1_760_000_000  # fixed epoch for deterministic phases / creation times
# Restart counters count boundaries crossed since ~6h before this process started,
# so the demo shows realistic small numbers instead of counting from T0.
R0 = (int(time.time()) // 3600) * 3600 - 6 * 3600


@dataclass(frozen=True)
class _Spec:
    name: str
    service: str
    kind: str                # steady | leak | crashloop | spiky | pending | notready
    seed: int
    cpu_base: float          # millicores (app container)
    cpu_amp: float
    cpu_lim: float
    cpu_req: float
    mem_base_mib: float
    mem_lim_mib: float
    mem_req_mib: float
    node: Optional[str]
    sidecar: bool = False
    age_days: float = 3.0
    leak_period_s: int = 2400


_SPECS: List[_Spec] = [
    _Spec("checkout-service-6d8f9c7b5-x7k2p", "checkout-service", "steady", 1, 180, 60, 500, 250, 210, 512, 256, "node-a", True),
    _Spec("checkout-service-6d8f9c7b5-m9q4z", "checkout-service", "steady", 2, 170, 55, 500, 250, 205, 512, 256, "node-b", True),
    _Spec("checkout-service-6d8f9c7b5-t2w8n", "checkout-service", "spiky", 3, 150, 40, 500, 250, 215, 512, 256, "node-b", True),
    _Spec("payments-api-5b7c8d9f4-h4v6r", "payments-api", "leak", 4, 120, 30, 400, 200, 170, 512, 256, "node-a", False, 1.0, 2400),
    _Spec("payments-api-5b7c8d9f4-p8d2s", "payments-api", "steady", 5, 115, 28, 400, 200, 180, 512, 256, "node-c"),
    _Spec("search-indexer-7f6c5d8b9-k3j9x", "search-indexer", "crashloop", 6, 25, 20, 300, 100, 60, 256, 128, "node-c"),
    _Spec("frontend-web-84c7d6f5b-a1b2c", "frontend-web", "steady", 7, 40, 15, 200, 50, 88, 256, 128, "node-a"),
    _Spec("frontend-web-84c7d6f5b-d3e4f", "frontend-web", "steady", 8, 44, 16, 200, 50, 92, 256, 128, "node-c"),
    _Spec("notifier-5c9d8b7f6-w5z8q", "notifier", "notready", 9, 12, 6, 200, 50, 70, 256, 128, "node-b"),
    _Spec("report-worker-0", "report-worker", "pending", 10, 0, 0, 1000, 500, 0, 1024, 512, None, False, 0.014),
]
_BY_NAME = {s.name: s for s in _SPECS}


def _jitter(seed: int, t: float, bucket: int = 5) -> float:
    """Deterministic pseudo-random in [-1, 1] per 5s bucket."""
    h = (int(t // bucket) * 2654435761 + seed * 40503) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 2246822519) & 0xFFFFFFFF
    return (h / 0xFFFFFFFF) * 2 - 1


def _wave(seed: int, t: float) -> float:
    return 0.5 * math.sin(t / 1100 + seed) + 0.3 * math.sin(t / 310 + seed * 1.7) + 0.2 * math.sin(t / 97 + seed * 2.3)


def _phase(spec: _Spec, t: float) -> float:
    return (t - T0 + spec.seed * 211) % spec.leak_period_s


def _app_usage(spec: _Spec, t: float) -> Optional[tuple[float, float]]:
    """(cpu_m, mem_b) of the main container at time t, or None if no data."""
    if spec.kind == "pending":
        return None
    cpu = spec.cpu_base + spec.cpu_amp * _wave(spec.seed, t) + 6 * _jitter(spec.seed, t)
    mem = spec.mem_base_mib * MIB * (1 + 0.02 * _wave(spec.seed + 3, t)) + 0.4 * MIB * _jitter(spec.seed + 1, t)
    if spec.kind == "spiky":
        if (t - T0) % 420 < 90:
            cpu = spec.cpu_lim * (0.93 + 0.06 * _jitter(spec.seed, t))
    elif spec.kind == "leak":
        frac = _phase(spec, t) / spec.leak_period_s
        mem = spec.mem_base_mib * MIB + (spec.mem_lim_mib * 0.985 - spec.mem_base_mib) * MIB * frac
        cpu += 40 * frac
    elif spec.kind == "crashloop":
        p = (t - T0 + spec.seed * 31) % 300
        cpu = 5 + (spec.cpu_amp * 2 if p < 20 else 0) + 2 * abs(_jitter(spec.seed, t))
        mem = (30 + 25 * min(p, 60) / 60) * MIB
    return max(cpu, 0.5), max(mem, 8 * MIB)


def _sidecar_usage(spec: _Spec, app: tuple[float, float], t: float) -> tuple[float, float]:
    return 3 + 0.12 * app[0], 38 * MIB + 0.5 * MIB * _jitter(spec.seed + 7, t)


def _restarts(spec: _Spec, t: float) -> int:
    if spec.kind == "leak":
        off, period, base = spec.seed * 211, spec.leak_period_s, 1
    elif spec.kind == "crashloop":
        off, period, base = spec.seed * 31, 300, 3
    else:
        return 0
    crossed = int((t - T0 + off) // period) - int((R0 - T0 + off) // period)
    return base + max(0, crossed)


def _last_term(spec: _Spec, t: float) -> tuple[Optional[str], Optional[float], Optional[int]]:
    if spec.kind == "leak":
        return "OOMKilled", t - _phase(spec, t), 137
    if spec.kind == "crashloop":
        return "Error", t - ((t - T0 + spec.seed * 31) % 300), 1
    return None, None, None


def _containers(spec: _Spec, t: float) -> List[ContainerSnapshot]:
    app = _app_usage(spec, t)
    reason, finished, code = _last_term(spec, t)
    ready = spec.kind not in ("pending", "crashloop", "notready")
    state, creason, cmsg = "running", None, None
    if spec.kind == "crashloop":
        state, creason, cmsg = "waiting", "CrashLoopBackOff", "back-off 5m0s restarting failed container"
    elif spec.kind == "pending":
        state, creason, cmsg = "waiting", "ContainerCreating", None
    main = ContainerSnapshot(
        name="app", ready=ready, restart_count=_restarts(spec, t), state=state, reason=creason, message=cmsg,
        last_termination_reason=reason, last_termination_exit_code=code, last_termination_at=finished,
        cpu_m=app[0] if app else None, mem_b=app[1] if app else None,
        cpu_req_m=spec.cpu_req, cpu_lim_m=spec.cpu_lim,
        mem_req_b=spec.mem_req_mib * MIB, mem_lim_b=spec.mem_lim_mib * MIB,
    )
    out = [main]
    if spec.sidecar:
        sc = _sidecar_usage(spec, app, t) if app else (None, None)
        out.append(ContainerSnapshot(
            name="envoy-proxy", ready=True, state="running", cpu_m=sc[0], mem_b=sc[1],
            cpu_req_m=50, cpu_lim_m=100, mem_req_b=64 * MIB, mem_lim_b=128 * MIB,
        ))
    return out


def _snapshot(spec: _Spec, now: float) -> PodSnapshot:
    cs = _containers(spec, now)
    ready_n = sum(1 for c in cs if c.ready)
    reason = next((c.reason for c in cs if c.reason), None)
    message = next((c.message for c in cs if c.message), None)
    reason_t = _last_term(spec, now)
    has_usage = all(c.cpu_m is not None for c in cs)

    def total(attr: str) -> Optional[float]:
        vals = [getattr(c, attr) for c in cs]
        return float(sum(vals)) if vals and all(v is not None for v in vals) else None

    created = now - spec.age_days * 86400 if spec.kind == "pending" else T0 - 3600 * 24 * spec.age_days
    return PodSnapshot(
        namespace=NAMESPACE, name=spec.name, service=spec.service,
        phase="Pending" if spec.kind == "pending" else "Running",
        ready=ready_n == len(cs), ready_containers=f"{ready_n}/{len(cs)}",
        restart_count=sum(c.restart_count for c in cs), node_name=spec.node,
        pod_ip=None if spec.kind == "pending" else f"10.244.{spec.seed}.{10 + spec.seed}",
        created_at=created, reason=reason,
        message=message or ("0/3 nodes are available: insufficient cpu." if spec.kind == "pending" else None),
        last_termination_reason=reason_t[0], last_termination_at=reason_t[1],
        cpu_m=sum(c.cpu_m for c in cs) if has_usage else None,
        mem_b=sum(c.mem_b for c in cs) if has_usage else None,
        cpu_req_m=total("cpu_req_m"), cpu_lim_m=total("cpu_lim_m"),
        mem_req_b=total("mem_req_b"), mem_lim_b=total("mem_lim_b"),
        containers=cs, labels={"app": spec.service},
    )


# ---------------------------------------------------------------------------
# Public API (mirrors k8s_source)
# ---------------------------------------------------------------------------
def list_namespaces() -> List[str]:
    return [NAMESPACE]


def list_services(namespace: str = NAMESPACE) -> List[str]:
    return sorted({s.service for s in _SPECS}) if namespace == NAMESPACE else []


def snapshot(namespace: str, service: Optional[str] = None, now: Optional[float] = None) -> List[PodSnapshot]:
    if namespace != NAMESPACE:
        return []
    now = now or time.time()
    return [_snapshot(s, now) for s in _SPECS if not service or s.service == service]


def _net(spec: _Spec, cpu: float, t: float) -> tuple[float, float]:
    rx = max(0.0, cpu * 900 * (1 + 0.15 * _jitter(spec.seed + 11, t)))
    return rx, rx * 0.7


def series(namespace: str, pods: Sequence[str], start: float, end: float, step: int, now: Optional[float] = None) -> SeriesResult:
    a, b, ts = grid(start, end, step)
    metrics: Dict[str, Dict[str, List[Optional[float]]]] = {
        k: {} for k in ("cpu_m", "mem_b", "restarts", "net_rx_bps", "net_tx_bps", "cpu_throttle_pct")
    }
    for name in pods:
        spec = _BY_NAME.get(name)
        cols: Dict[str, List[Optional[float]]] = {k: [] for k in metrics}
        for t in ts:
            app = _app_usage(spec, t) if spec else None
            if not app or not spec:
                for k in cols:
                    cols[k].append(None)
                continue
            total_cpu, total_mem = app
            if spec.sidecar:
                sc = _sidecar_usage(spec, app, t)
                total_cpu, total_mem = total_cpu + sc[0], total_mem + sc[1]
            rx, tx = _net(spec, total_cpu, t)
            lim_ratio = app[0] / spec.cpu_lim
            cols["cpu_m"].append(total_cpu)
            cols["mem_b"].append(total_mem)
            cols["restarts"].append(float(_restarts(spec, t)))
            cols["net_rx_bps"].append(rx)
            cols["net_tx_bps"].append(tx)
            cols["cpu_throttle_pct"].append(max(0.0, (lim_ratio - 0.88) * 400) if lim_ratio > 0.88 else 0.0)
        for k in metrics:
            metrics[k][name] = cols[k]
    return SeriesResult(start=a, end=b, step=step, timestamps=ts, pods=list(pods), metrics=metrics,
                        source="demo", history_seconds=b - a)


def container_series(namespace: str, pod: str, containers: Sequence[str], start: float, end: float, step: int) -> SeriesResult:
    a, b, ts = grid(start, end, step)
    spec = _BY_NAME.get(pod)
    cpu = {c: [] for c in containers}
    mem = {c: [] for c in containers}
    for t in ts:
        app = _app_usage(spec, t) if spec else None
        for c in containers:
            if not app or not spec:
                cpu[c].append(None)
                mem[c].append(None)
            elif c == "app":
                cpu[c].append(app[0])
                mem[c].append(app[1])
            else:
                sc = _sidecar_usage(spec, app, t)
                cpu[c].append(sc[0])
                mem[c].append(sc[1])
    return SeriesResult(start=a, end=b, step=step, timestamps=ts, pods=list(containers),
                        metrics={"cpu_m": cpu, "mem_b": mem}, source="demo", history_seconds=b - a)


def events(namespace: str, pod: str, now: Optional[float] = None) -> List[dict]:
    now = now or time.time()
    spec = _BY_NAME.get(pod)
    if not spec:
        return []
    ev: List[dict] = []

    def add(type_: str, reason: str, msg: str, ago: float, count: int = 1, src: str = "kubelet"):
        ev.append({"type": type_, "reason": reason, "message": msg, "count": count, "last_seen": now - ago, "source": src})

    if spec.kind == "pending":
        add("Warning", "FailedScheduling", "0/3 nodes are available: 3 Insufficient cpu.", 600, 12, "default-scheduler")
    else:
        add("Normal", "Scheduled", f"Successfully assigned demo/{pod} to {spec.node}", 86400, 1, "default-scheduler")
        add("Normal", "Pulled", "Container image already present on machine", 86390)
        add("Normal", "Started", "Started container app", 86385)
    if spec.kind == "leak":
        add("Warning", "OOMKilling", "Memory cgroup out of memory: Killed process (app)", _phase(spec, now), 1, "kernel")
        add("Warning", "BackOff", "Back-off restarting failed container app", _phase(spec, now) + 30, 3)
    elif spec.kind == "crashloop":
        add("Warning", "BackOff", "Back-off restarting failed container app in pod", 40, 58)
        add("Warning", "Failed", "Error: container exited with code 1", 120, 6)
    elif spec.kind == "notready":
        add("Warning", "Unhealthy", "Readiness probe failed: HTTP probe failed with statuscode: 503", 20, 144)
    return sorted(ev, key=lambda e: e["last_seen"], reverse=True)


def logs(namespace: str, pod: str, container: Optional[str], lines: int, now: Optional[float] = None) -> str:
    now = now or time.time()
    spec = _BY_NAME.get(pod)
    if not spec:
        return ""
    if spec.kind == "pending":
        return "[no logs available: container has not started]"

    def stamp(ago: float) -> str:
        return datetime.fromtimestamp(now - ago, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = ["# DEMO DATA - synthetic log lines"]
    for i in range(min(lines, 30), 0, -1):
        ago = i * 7
        if spec.kind == "crashloop":
            msg = "ERROR indexer: cannot start - KeyError: 'INDEX_BUCKET' (env var missing)"
        elif spec.kind == "leak" and i < 12:
            msg = f"WARN heap usage {70 + (12 - i) * 2}% - cache entries not evicted"
        elif spec.kind == "notready" and i % 3 == 0:
            msg = "WARN readiness check failed: upstream smtp-relay timeout"
        else:
            msg = f"INFO {spec.service} handled request id={1000 + i * 7} status=200 ms={20 + (i * 13) % 80}"
        out.append(f"{stamp(ago)} {msg}")
    return "\n".join(out)
