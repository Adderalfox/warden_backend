"""In-process rolling history of pod samples (the "TSDB" used when Prometheus
isn't configured). Samples come from the collector's periodic scrapes of the
Kubernetes API + metrics-server."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from app.metrics.models import PodSnapshot, SeriesResult


@dataclass
class Sample:
    ts: float
    cpu_m: Optional[float]
    mem_b: Optional[float]
    restarts: int
    containers: Dict[str, Tuple[Optional[float], Optional[float]]] = field(default_factory=dict)


def grid(start: float, end: float, step: int) -> Tuple[float, float, List[float]]:
    """Align [start, end] to multiples of ``step`` and return the timestamp grid."""
    a = (int(start) // step) * step
    b = (int(end) // step) * step
    return a, b, [float(t) for t in range(a, b + 1, step)]


class SampleStore:
    def __init__(self, retention_seconds: int = 6 * 3600) -> None:
        self._retention = retention_seconds
        self._data: Dict[Tuple[str, str], Deque[Sample]] = {}
        self._lock = threading.Lock()

    # --- write ---
    def record(self, namespace: str, pods: Iterable[PodSnapshot], ts: float) -> None:
        with self._lock:
            for p in pods:
                dq = self._data.setdefault((namespace, p.name), deque())
                dq.append(
                    Sample(
                        ts=ts, cpu_m=p.cpu_m, mem_b=p.mem_b, restarts=p.restart_count,
                        containers={c.name: (c.cpu_m, c.mem_b) for c in p.containers},
                    )
                )
            self._prune_locked(ts)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._retention
        for key in list(self._data):
            dq = self._data[key]
            while dq and dq[0].ts < cutoff:
                dq.popleft()
            if not dq:
                del self._data[key]

    # --- read ---
    def first_ts(self, namespace: str, pods: Sequence[str]) -> Optional[float]:
        with self._lock:
            firsts = [self._data[(namespace, p)][0].ts for p in pods if (namespace, p) in self._data]
        return min(firsts) if firsts else None

    def series(self, namespace: str, pods: Sequence[str], start: float, end: float, step: int) -> SeriesResult:
        a, b, ts = grid(start, end, step)
        n = len(ts)
        out: Dict[str, Dict[str, List[Optional[float]]]] = {"cpu_m": {}, "mem_b": {}, "restarts": {}}
        with self._lock:
            snapshot = {p: list(self._data.get((namespace, p), ())) for p in pods}
        first_seen = min((s[0].ts for s in snapshot.values() if s), default=None)
        for pod, samples in snapshot.items():
            cpu_sum, cpu_n = [0.0] * n, [0] * n
            mem_sum, mem_n = [0.0] * n, [0] * n
            rst: List[Optional[float]] = [None] * n
            for s in samples:
                if s.ts < a or s.ts >= b + step:
                    continue
                i = int((s.ts - a) // step)
                if s.cpu_m is not None:
                    cpu_sum[i] += s.cpu_m
                    cpu_n[i] += 1
                if s.mem_b is not None:
                    mem_sum[i] += s.mem_b
                    mem_n[i] += 1
                rst[i] = float(s.restarts) if rst[i] is None else max(rst[i], float(s.restarts))
            out["cpu_m"][pod] = [cpu_sum[i] / cpu_n[i] if cpu_n[i] else None for i in range(n)]
            out["mem_b"][pod] = [mem_sum[i] / mem_n[i] if mem_n[i] else None for i in range(n)]
            out["restarts"][pod] = rst
        history = (b - first_seen) if first_seen is not None else 0.0
        return SeriesResult(
            start=a, end=b, step=step, timestamps=ts, pods=list(pods), metrics=out,
            source="collector", history_seconds=max(0.0, history),
        )

    def container_series(self, namespace: str, pod: str, containers: Sequence[str], start: float, end: float, step: int) -> SeriesResult:
        a, b, ts = grid(start, end, step)
        n = len(ts)
        with self._lock:
            samples = list(self._data.get((namespace, pod), ()))
        acc = {c: {"cpu": ([0.0] * n, [0] * n), "mem": ([0.0] * n, [0] * n)} for c in containers}
        for s in samples:
            if s.ts < a or s.ts >= b + step:
                continue
            i = int((s.ts - a) // step)
            for c in containers:
                cpu, mem = s.containers.get(c, (None, None))
                if cpu is not None:
                    acc[c]["cpu"][0][i] += cpu
                    acc[c]["cpu"][1][i] += 1
                if mem is not None:
                    acc[c]["mem"][0][i] += mem
                    acc[c]["mem"][1][i] += 1

        def avg(pair):
            tot, cnt = pair
            return [tot[i] / cnt[i] if cnt[i] else None for i in range(n)]

        metrics = {
            "cpu_m": {c: avg(acc[c]["cpu"]) for c in containers},
            "mem_b": {c: avg(acc[c]["mem"]) for c in containers},
        }
        history = (b - samples[0].ts) if samples else 0.0
        return SeriesResult(
            start=a, end=b, step=step, timestamps=ts, pods=list(containers), metrics=metrics,
            source="collector", history_seconds=max(0.0, history),
        )
