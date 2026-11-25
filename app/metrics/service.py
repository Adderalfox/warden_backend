"""
MetricsService: the single entry point the API and MCP tools use.

* Live pod table + status        <- Kubernetes API (+ metrics-server usage)
* Time series                    <- Prometheus if WARDEN_PROMETHEUS_URL is set,
                                    else the in-process collector's rolling history
* Demo mode (opt-in)             <- synthetic cluster, flagged everywhere
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from app.core.config import settings
from app.metrics import demo, k8s_source
from app.metrics.insights import compute_insights, summarize
from app.metrics.models import PodSnapshot, SeriesResult
from app.metrics.prometheus import PrometheusClient, PrometheusError
from app.metrics.store import SampleStore, grid

log = logging.getLogger("warden.metrics")

RANGES = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "3h": 10800, "6h": 21600, "12h": 43200, "24h": 86400}
MAX_POINTS = 240


class MetricsError(RuntimeError):
    """User-facing analytics error (cluster unreachable, bad range, ...)."""


def parse_range(value: str) -> int:
    if value in RANGES:
        return RANGES[value]
    try:  # allow "90m", "2h"
        n, unit = int(value[:-1]), value[-1]
        seconds = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        if 60 <= seconds <= 7 * 86400:
            return seconds
    except (ValueError, KeyError, IndexError):
        pass
    raise MetricsError(f"Invalid range '{value}'. Use one of {', '.join(RANGES)}.")


class MetricsService:
    def __init__(self) -> None:
        self.store = SampleStore(retention_seconds=settings.metrics_retention_minutes * 60)
        self._watched: set[str] = set()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.last_scrape_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self._prom: Optional[PrometheusClient] = (
            PrometheusClient(settings.prometheus_url) if settings.prometheus_url else None
        )

    # ------------------------------------------------------------------
    # configuration helpers
    # ------------------------------------------------------------------
    @property
    def demo(self) -> bool:
        return settings.metrics_source == "demo"

    @property
    def series_source(self) -> str:
        if self.demo:
            return "demo"
        return "prometheus" if self._prom else "collector"

    def _configured_namespaces(self) -> List[str]:
        raw = settings.metrics_namespaces.strip()
        if raw == "*":
            try:
                return k8s_source.list_namespaces() or [settings.k8s_namespace_default]
            except k8s_source.ClusterUnavailable:
                return [settings.k8s_namespace_default]
        if raw:
            return [n.strip() for n in raw.split(",") if n.strip()]
        return [settings.k8s_namespace_default]

    # ------------------------------------------------------------------
    # collector lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self.demo or not settings.metrics_enabled or self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="warden-metrics-collector")

    async def stop(self) -> None:
        if self._task:
            self._stop.set()
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.scrape_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.metrics_scrape_interval_seconds)
            except asyncio.TimeoutError:
                pass

    def _scrape_namespace(self, namespace: str) -> None:
        pods, _ = k8s_source.snapshot(namespace)
        self.store.record(namespace, pods, time.time())

    async def scrape_once(self) -> None:
        try:
            namespaces = set(self._configured_namespaces()) | self._watched
            for ns in sorted(namespaces):
                await asyncio.to_thread(self._scrape_namespace, ns)
            self.last_scrape_at = time.time()
            self.last_error = None
        except Exception as e:  # noqa: BLE001 - keep the loop alive, surface in /status
            self.last_error = str(e)
            log.warning("metrics scrape failed: %s", e)

    async def ensure_namespace(self, namespace: str) -> None:
        """Start collecting a namespace the user picked in the UI."""
        if self.demo or namespace in self._watched or namespace in self._configured_namespaces():
            return
        self._watched.add(namespace)
        try:
            await asyncio.to_thread(self._scrape_namespace, namespace)
        except Exception as e:  # noqa: BLE001
            log.warning("initial scrape of %s failed: %s", namespace, e)

    # ------------------------------------------------------------------
    # status / discovery
    # ------------------------------------------------------------------
    async def status(self) -> dict:
        prom_ok: Optional[bool] = None
        prom_err: Optional[str] = None
        if self._prom:
            try:
                await self._prom.ping()
                prom_ok = True
            except PrometheusError as e:
                prom_ok, prom_err = False, str(e)
        return {
            "source": settings.metrics_source,
            "synthetic": self.demo,
            "series_source": self.series_source,
            "collector_running": bool(self._task and not self._task.done()),
            "scrape_interval_seconds": settings.metrics_scrape_interval_seconds,
            "retention_minutes": settings.metrics_retention_minutes,
            "namespaces_collected": sorted(set(self._configured_namespaces()) | self._watched) if not self.demo else [demo.NAMESPACE],
            "last_scrape_at": self.last_scrape_at,
            "last_error": self.last_error,
            "prometheus": {"configured": bool(self._prom), "reachable": prom_ok, "error": prom_err},
            "available_ranges": list(RANGES),
        }

    async def namespaces(self) -> List[str]:
        if self.demo:
            return demo.list_namespaces()
        try:
            found = await asyncio.to_thread(k8s_source.list_namespaces)
        except k8s_source.ClusterUnavailable as e:
            raise MetricsError(str(e)) from e
        return found or sorted(set(self._configured_namespaces()) | self._watched)

    async def services(self, namespace: str) -> List[str]:
        if self.demo:
            return demo.list_services(namespace)
        try:
            return await asyncio.to_thread(k8s_source.list_services, namespace)
        except k8s_source.ClusterUnavailable as e:
            raise MetricsError(str(e)) from e

    # ------------------------------------------------------------------
    # snapshots + series
    # ------------------------------------------------------------------
    async def snapshot(self, namespace: str, service: Optional[str] = None):
        if self.demo:
            return demo.snapshot(namespace, service), None
        try:
            return await asyncio.to_thread(k8s_source.snapshot, namespace, service)
        except k8s_source.ClusterUnavailable as e:
            raise MetricsError(str(e)) from e

    def _step_for(self, range_s: int) -> int:
        base = 15 if self.demo else max(5, settings.metrics_scrape_interval_seconds)
        step = max(base, -(-range_s // MAX_POINTS))
        return -(-step // base) * base  # round up to a multiple of the scrape interval

    async def _series(self, namespace: str, pods: List[str], range_s: int) -> SeriesResult:
        end = time.time()
        step = self._step_for(range_s)
        start = end - range_s
        if self.demo:
            return demo.series(namespace, pods, start, end, step)
        if self._prom:
            try:
                return await self._prom.pod_series(namespace, pods, start, end, step)
            except PrometheusError as e:
                self.last_error = f"Prometheus: {e}"
                log.warning("prometheus series failed, falling back to collector: %s", e)
        res = self.store.series(namespace, pods, start, end, step)
        # Don't show a mostly-empty chart: clip the window to the history we actually have.
        first = self.store.first_ts(namespace, pods)
        if first is not None and first > res.start + step:
            a, b, ts = grid(max(first - step, start), end, step)
            res = self.store.series(namespace, pods, a, b, step)
        return res

    async def dashboard(self, namespace: str, service: Optional[str], range_s: int) -> dict:
        await self.ensure_namespace(namespace)
        pods, warning = await self.snapshot(namespace, service)
        names = [p.name for p in pods]
        series = await self._series(namespace, names, range_s) if names else None
        insights = compute_insights(pods, series, metrics_warning=warning)
        limits: Dict[str, dict] = {
            p.name: {"cpu_req_m": p.cpu_req_m, "cpu_lim_m": p.cpu_lim_m,
                     "mem_req_b": p.mem_req_b, "mem_lim_b": p.mem_lim_b}
            for p in pods
        }
        return {
            "namespace": namespace,
            "service": service,
            "range_seconds": range_s,
            "generated_at": time.time(),
            "source": {
                "kind": settings.metrics_source,
                "series_source": self.series_source if series is None else series.source,
                "synthetic": self.demo,
                "warning": warning,
                "last_error": self.last_error,
            },
            "summary": summarize(pods, series),
            "pods": [p.model_dump() for p in pods],
            "series": series.as_dict() if series else None,
            "limits": limits,
            "insights": [i.model_dump() for i in insights],
            "history": {
                "available_seconds": series.history_seconds if series else 0,
                "requested_seconds": range_s,
            },
        }

    async def pod_detail(self, namespace: str, pod_name: str, range_s: int) -> dict:
        await self.ensure_namespace(namespace)
        pods, warning = await self.snapshot(namespace, None)
        pod = next((p for p in pods if p.name == pod_name), None)
        if pod is None:
            raise LookupError(f"Pod '{pod_name}' not found in namespace '{namespace}'")
        series = await self._series(namespace, [pod_name], range_s)
        end, step = time.time(), self._step_for(range_s)
        cnames = [c.name for c in pod.containers]
        if self.demo:
            cseries = demo.container_series(namespace, pod_name, cnames, end - range_s, end, step)
        elif self._prom:
            try:
                cseries = await self._prom.container_series(namespace, pod_name, cnames, end - range_s, end, step)
            except PrometheusError:
                cseries = self.store.container_series(namespace, pod_name, cnames, end - range_s, end, step)
        else:
            cseries = self.store.container_series(namespace, pod_name, cnames, max(end - range_s, series.start), end, step)
        return {
            "pod": pod.model_dump(),
            "series": series.as_dict(),
            "container_series": cseries.as_dict(),
            "insights": [i.model_dump() for i in compute_insights([pod], series, metrics_warning=warning)],
            "source": {"kind": settings.metrics_source, "series_source": series.source,
                       "synthetic": self.demo, "warning": warning},
            "range_seconds": range_s,
            "generated_at": time.time(),
        }

    async def events(self, namespace: str, pod: str) -> List[dict]:
        if self.demo:
            return demo.events(namespace, pod)
        try:
            return await asyncio.to_thread(k8s_source.pod_events, namespace, pod)
        except k8s_source.ClusterUnavailable as e:
            raise MetricsError(str(e)) from e

    async def logs(self, namespace: str, pod: str, container: Optional[str], lines: int, previous: bool = False) -> str:
        if self.demo:
            return demo.logs(namespace, pod, container, lines)
        try:
            return await asyncio.to_thread(k8s_source.pod_logs, namespace, pod, container, lines, previous)
        except k8s_source.ClusterUnavailable as e:
            raise MetricsError(str(e)) from e

    async def diagnostic_snapshot(self, namespace: str, service: str) -> dict:
        """Compact, LLM/agent-friendly summary used by the mcp-k8s tool."""
        pods, warning = await self.snapshot(namespace, service)
        insights = compute_insights(pods, None, metrics_warning=warning)

        def r(v: Optional[float], nd: int = 1) -> Optional[float]:
            return None if v is None else round(v, nd)

        return {
            "namespace": namespace,
            "service_name": service,
            "found": bool(pods),
            "synthetic": self.demo,
            "metrics_warning": warning,
            "summary": summarize(pods),
            "pods": [
                {
                    "pod_name": p.name, "phase": p.phase, "ready": p.ready, "restart_count": p.restart_count,
                    "reason": p.reason, "last_termination_reason": p.last_termination_reason,
                    "cpu_m": r(p.cpu_m), "cpu_lim_m": r(p.cpu_lim_m),
                    "mem_mib": r(p.mem_b / 2**20) if p.mem_b is not None else None,
                    "mem_lim_mib": r(p.mem_lim_b / 2**20) if p.mem_lim_b else None,
                    "cpu_pct_of_limit": r(100 * p.cpu_m / p.cpu_lim_m) if p.cpu_m is not None and p.cpu_lim_m else None,
                    "mem_pct_of_limit": r(100 * p.mem_b / p.mem_lim_b) if p.mem_b is not None and p.mem_lim_b else None,
                }
                for p in pods
            ],
            "insights": [i.model_dump() for i in insights],
        }


metrics_service = MetricsService()
