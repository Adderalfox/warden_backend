"""
Optional Prometheus backend for long-range history, network and CPU-throttling
panels. Enabled by setting ``WARDEN_PROMETHEUS_URL``. Uses the standard
cAdvisor / kube-state-metrics series that kube-prometheus-stack ships with.
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Sequence

import httpx

from app.metrics.models import SeriesResult

POD_METRICS = ("cpu_m", "mem_b", "restarts", "net_rx_bps", "net_tx_bps", "cpu_throttle_pct")
CONTAINER_METRICS = ("cpu_m", "mem_b")


class PrometheusError(RuntimeError):
    pass


def _q(value: str) -> str:
    """Escape a value for use inside a double-quoted PromQL string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _pod_regex(pods: Sequence[str]) -> str:
    return _q("|".join(re.escape(p) for p in pods))


def _rate_window(step: int) -> str:
    return f"{max(60, step * 4)}s"


def pod_queries(namespace: str, pods: Sequence[str], step: int) -> Dict[str, str]:
    ns, rx, w = _q(namespace), _pod_regex(pods), _rate_window(step)
    sel = f'namespace="{ns}",pod=~"{rx}"'
    csel = f'{sel},container!="",container!="POD"'
    return {
        "cpu_m": f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{csel}}}[{w}])) * 1000",
        "mem_b": f"sum by (pod) (container_memory_working_set_bytes{{{csel}}})",
        "restarts": f"sum by (pod) (kube_pod_container_status_restarts_total{{{sel}}})",
        "net_rx_bps": f"sum by (pod) (rate(container_network_receive_bytes_total{{{sel}}}[{w}]))",
        "net_tx_bps": f"sum by (pod) (rate(container_network_transmit_bytes_total{{{sel}}}[{w}]))",
        "cpu_throttle_pct": (
            f"100 * sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{csel}}}[{w}])) "
            f"/ sum by (pod) (rate(container_cpu_cfs_periods_total{{{csel}}}[{w}]))"
        ),
    }


def container_queries(namespace: str, pod: str, step: int) -> Dict[str, str]:
    ns, p, w = _q(namespace), _q(pod), _rate_window(step)
    csel = f'namespace="{ns}",pod="{p}",container!="",container!="POD"'
    return {
        "cpu_m": f"sum by (container) (rate(container_cpu_usage_seconds_total{{{csel}}}[{w}])) * 1000",
        "mem_b": f"sum by (container) (container_memory_working_set_bytes{{{csel}}})",
    }


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 10.0, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def _range(self, query: str, start: float, end: float, step: int, label: str) -> Dict[str, List[Optional[float]]]:
        n = int((end - start) // step) + 1
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
            try:
                r = await c.get(
                    f"{self._base}/api/v1/query_range",
                    params={"query": query, "start": start, "end": end, "step": step},
                )
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise PrometheusError(f"Prometheus request failed: {e}") from e
        body = r.json()
        if body.get("status") != "success":
            raise PrometheusError(f"Prometheus error: {body.get('error', body)}")
        out: Dict[str, List[Optional[float]]] = {}
        for series in body["data"]["result"]:
            key = series["metric"].get(label)
            if key is None:
                continue
            vals: List[Optional[float]] = [None] * n
            for ts, raw in series["values"]:
                idx = int(round((float(ts) - start) / step))
                if 0 <= idx < n:
                    v = float(raw)
                    vals[idx] = None if (math.isnan(v) or math.isinf(v)) else v
            out[key] = vals
        return out

    async def pod_series(self, namespace: str, pods: Sequence[str], start: float, end: float, step: int) -> SeriesResult:
        timestamps = [start + i * step for i in range(int((end - start) // step) + 1)]
        metrics: Dict[str, Dict[str, List[Optional[float]]]] = {}
        if pods:
            for name, promql in pod_queries(namespace, pods, step).items():
                try:
                    data = await self._range(promql, start, end, step, "pod")
                except PrometheusError:
                    if name in ("cpu_m", "mem_b"):
                        raise  # core panels must work; optional ones may be absent
                    data = {}
                metrics[name] = {p: data.get(p, [None] * len(timestamps)) for p in pods}
        return SeriesResult(
            start=start, end=end, step=step, timestamps=timestamps, pods=list(pods),
            metrics=metrics, source="prometheus", history_seconds=end - start,
        )

    async def container_series(self, namespace: str, pod: str, containers: Sequence[str], start: float, end: float, step: int) -> SeriesResult:
        timestamps = [start + i * step for i in range(int((end - start) // step) + 1)]
        metrics: Dict[str, Dict[str, List[Optional[float]]]] = {}
        for name, promql in container_queries(namespace, pod, step).items():
            data = await self._range(promql, start, end, step, "container")
            metrics[name] = {c: data.get(c, [None] * len(timestamps)) for c in containers}
        return SeriesResult(
            start=start, end=end, step=step, timestamps=timestamps, pods=list(containers),
            metrics=metrics, source="prometheus", history_seconds=end - start,
        )

    async def ping(self) -> None:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
            try:
                (await c.get(f"{self._base}/-/ready")).raise_for_status()
            except httpx.HTTPError as e:
                raise PrometheusError(f"Prometheus not reachable at {self._base}: {e}") from e
