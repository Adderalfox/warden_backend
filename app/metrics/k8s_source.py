"""
Real Kubernetes data source for the analytics layer.

* Pod metadata / status / resource requests+limits  -> CoreV1 API
* Live CPU + memory usage                           -> ``metrics.k8s.io`` (metrics-server),
  the same data ``kubectl top pods`` shows
* Events and logs                                   -> CoreV1 API

All calls are blocking (the official client is synchronous); callers run them
via ``asyncio.to_thread``. Nothing here fabricates data -- if the cluster or
metrics-server is unreachable a typed error is raised and surfaced in the UI.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from app.core.config import settings
from app.metrics.models import ContainerSnapshot, PodSnapshot
from app.metrics.quantity import cpu_millicores, mem_bytes

SERVICE_LABELS = ("app", "app.kubernetes.io/name")

_lock = threading.Lock()
_core: Optional[k8s_client.CoreV1Api] = None
_custom: Optional[k8s_client.CustomObjectsApi] = None


class ClusterUnavailable(RuntimeError):
    """Kubernetes API could not be configured or reached."""


class MetricsServerUnavailable(RuntimeError):
    """The cluster is reachable but metrics.k8s.io is not serving data."""


# ---------------------------------------------------------------------------
# Client bootstrap
# ---------------------------------------------------------------------------
def _load_config() -> None:
    try:
        if settings.kubeconfig_path:
            k8s_config.load_kube_config(config_file=settings.kubeconfig_path)
            return
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
    except Exception as e:  # noqa: BLE001
        raise ClusterUnavailable(
            "No usable Kubernetes configuration found. Set WARDEN_KUBECONFIG_PATH, "
            "run inside a cluster, or make sure ~/.kube/config points at a cluster "
            f"(kubectl get pods should work). Underlying error: {e}"
        ) from e


def core_v1() -> k8s_client.CoreV1Api:
    global _core
    with _lock:
        if _core is None:
            _load_config()
            _core = k8s_client.CoreV1Api()
        return _core


def custom_objects() -> k8s_client.CustomObjectsApi:
    global _custom
    with _lock:
        if _custom is None:
            _load_config()
            _custom = k8s_client.CustomObjectsApi()
        return _custom


def reset_clients() -> None:
    """Forget cached clients (used after a connection failure so config reloads)."""
    global _core, _custom
    with _lock:
        _core = None
        _custom = None


def _api_error(action: str, e: Exception) -> ClusterUnavailable:
    if isinstance(e, ApiException):
        return ClusterUnavailable(f"Kubernetes API error while {action}: {e.status} {e.reason}")
    return ClusterUnavailable(f"Cannot reach the Kubernetes API while {action}: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _epoch(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


_RS_HASH = re.compile(r"-[a-z0-9]{8,10}$")


def service_of(pod) -> str:
    labels = pod.metadata.labels or {}
    for key in SERVICE_LABELS:
        if labels.get(key):
            return labels[key]
    for ref in pod.metadata.owner_references or []:
        return _RS_HASH.sub("", ref.name) if ref.kind == "ReplicaSet" else ref.name
    return pod.metadata.name


def _container_snapshot(spec_c, status_c, usage: Optional[Tuple[float, float]]) -> ContainerSnapshot:
    res = spec_c.resources
    req = (res.requests or {}) if res else {}
    lim = (res.limits or {}) if res else {}
    snap = ContainerSnapshot(
        name=spec_c.name,
        cpu_req_m=cpu_millicores(req.get("cpu")),
        cpu_lim_m=cpu_millicores(lim.get("cpu")),
        mem_req_b=mem_bytes(req.get("memory")),
        mem_lim_b=mem_bytes(lim.get("memory")),
    )
    if usage is not None:
        snap.cpu_m, snap.mem_b = usage
    if status_c is not None:
        snap.ready = bool(status_c.ready)
        snap.restart_count = status_c.restart_count or 0
        st = status_c.state
        if st and st.running:
            snap.state = "running"
        elif st and st.waiting:
            snap.state, snap.reason, snap.message = "waiting", st.waiting.reason, st.waiting.message
        elif st and st.terminated:
            snap.state, snap.reason, snap.message = (
                "terminated", st.terminated.reason, st.terminated.message,
            )
        last = status_c.last_state.terminated if status_c.last_state else None
        if last:
            snap.last_termination_reason = last.reason
            snap.last_termination_exit_code = last.exit_code
            snap.last_termination_at = _epoch(last.finished_at)
    return snap


def _sum(values: List[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    return float(sum(present)) if present else None


def pod_to_snapshot(pod, usage: Optional[Dict[str, Tuple[float, float]]]) -> PodSnapshot:
    statuses = {c.name: c for c in (pod.status.container_statuses or [])}
    containers = [
        _container_snapshot(c, statuses.get(c.name), (usage or {}).get(c.name) if usage is not None else None)
        for c in pod.spec.containers
    ]
    ready_n = sum(1 for c in containers if c.ready)

    # Most relevant reason: a waiting/terminated container reason, else pod reason.
    reason = next((c.reason for c in containers if c.reason and c.state in ("waiting", "terminated")), None)
    message = next((c.message for c in containers if c.message and c.state in ("waiting", "terminated")), None)
    if not reason and pod.status.reason:
        reason, message = pod.status.reason, pod.status.message
    last_term = max(
        (c for c in containers if c.last_termination_at), key=lambda c: c.last_termination_at, default=None
    )

    def total(attr: str) -> Optional[float]:
        # Pod-level request/limit only when *every* container defines one;
        # a partial sum would make "% of limit" misleading.
        vals = [getattr(c, attr) for c in containers]
        if not vals or any(v is None for v in vals):
            return None
        return float(sum(vals)) or None

    has_usage = usage is not None and any(c.cpu_m is not None for c in containers)
    return PodSnapshot(
        namespace=pod.metadata.namespace,
        name=pod.metadata.name,
        service=service_of(pod),
        phase=pod.status.phase or "Unknown",
        ready=bool(containers) and ready_n == len(containers),
        ready_containers=f"{ready_n}/{len(containers)}",
        restart_count=sum(c.restart_count for c in containers),
        node_name=pod.spec.node_name,
        pod_ip=pod.status.pod_ip,
        created_at=_epoch(pod.metadata.creation_timestamp),
        reason=reason,
        message=message,
        last_termination_reason=last_term.last_termination_reason if last_term else None,
        last_termination_at=last_term.last_termination_at if last_term else None,
        cpu_m=_sum([c.cpu_m for c in containers]) if has_usage else None,
        mem_b=_sum([c.mem_b for c in containers]) if has_usage else None,
        cpu_req_m=total("cpu_req_m"),
        cpu_lim_m=total("cpu_lim_m"),
        mem_req_b=total("mem_req_b"),
        mem_lim_b=total("mem_lim_b"),
        containers=containers,
        labels=dict(pod.metadata.labels or {}),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def list_namespaces() -> List[str]:
    try:
        resp = core_v1().list_namespace()
    except ApiException as e:
        if e.status == 403:  # no cluster-wide list permission
            return []
        raise _api_error("listing namespaces", e) from e
    except ClusterUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - urllib3 connection errors etc.
        reset_clients()
        raise _api_error("listing namespaces", e) from e
    return sorted(ns.metadata.name for ns in resp.items)


def _list_pods(namespace: str, service: Optional[str]):
    api = core_v1()
    try:
        if not service:
            return api.list_namespaced_pod(namespace=namespace).items
        for key in SERVICE_LABELS:
            items = api.list_namespaced_pod(namespace=namespace, label_selector=f"{key}={service}").items
            if items:
                return items
        return []
    except ApiException as e:
        raise _api_error(f"listing pods in '{namespace}'", e) from e
    except ClusterUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        reset_clients()
        raise _api_error(f"listing pods in '{namespace}'", e) from e


def _pod_usage(namespace: str) -> Dict[str, Dict[str, Tuple[float, float]]]:
    try:
        resp = custom_objects().list_namespaced_custom_object(
            group="metrics.k8s.io", version="v1beta1", namespace=namespace, plural="pods"
        )
    except ApiException as e:
        if e.status in (404, 503):
            raise MetricsServerUnavailable(
                "metrics-server is not installed or not ready (the metrics.k8s.io API is not "
                "serving). Install it (minikube: `minikube addons enable metrics-server`) to get "
                "CPU/memory usage."
            ) from e
        if e.status == 403:
            raise MetricsServerUnavailable(
                "Not permitted to read metrics.k8s.io (RBAC). Grant 'get/list' on pods in "
                "metrics.k8s.io to the Warden service account."
            ) from e
        raise _api_error("reading pod metrics", e) from e
    out: Dict[str, Dict[str, Tuple[float, float]]] = {}
    for item in resp.get("items", []):
        per_container = {}
        for c in item.get("containers", []):
            usage = c.get("usage", {})
            per_container[c["name"]] = (
                cpu_millicores(usage.get("cpu", "0")) or 0.0,
                mem_bytes(usage.get("memory", "0")) or 0.0,
            )
        out[item["metadata"]["name"]] = per_container
    return out


def snapshot(namespace: str, service: Optional[str] = None) -> Tuple[List[PodSnapshot], Optional[str]]:
    """Return (pods, metrics_warning). ``metrics_warning`` is set when pods were
    listed fine but usage data could not be read."""
    pods = _list_pods(namespace, service)
    warning: Optional[str] = None
    usage: Optional[Dict[str, Dict[str, Tuple[float, float]]]] = None
    if pods:
        try:
            usage = _pod_usage(namespace)
        except MetricsServerUnavailable as e:
            warning = str(e)
        except ClusterUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            warning = f"Could not read pod metrics: {e}"
    snaps = [pod_to_snapshot(p, usage.get(p.metadata.name, {}) if usage is not None else None) for p in pods]
    if usage is not None:
        # A pod that just started has no metrics yet -> mark as no-data rather than 0.
        for s, p in zip(snaps, pods):
            if p.metadata.name not in usage:
                s.cpu_m = s.mem_b = None
    snaps.sort(key=lambda s: (s.service, s.name))
    return snaps, warning


def list_services(namespace: str) -> List[str]:
    return sorted({service_of(p) for p in _list_pods(namespace, None)})


def pod_events(namespace: str, pod: str, limit: int = 50) -> List[dict]:
    try:
        resp = core_v1().list_namespaced_event(
            namespace=namespace, field_selector=f"involvedObject.name={pod},involvedObject.kind=Pod"
        )
    except ApiException as e:
        raise _api_error(f"reading events for {pod}", e) from e
    except ClusterUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        reset_clients()
        raise _api_error(f"reading events for {pod}", e) from e

    def when(ev) -> float:
        return _epoch(ev.last_timestamp or ev.event_time or ev.first_timestamp or ev.metadata.creation_timestamp) or 0.0

    events = sorted(resp.items, key=when, reverse=True)[:limit]
    return [
        {
            "type": ev.type,
            "reason": ev.reason,
            "message": ev.message,
            "count": ev.count or 1,
            "last_seen": when(ev),
            "source": (ev.source.component if ev.source else None),
        }
        for ev in events
    ]


def pod_logs(namespace: str, pod: str, container: Optional[str] = None, lines: int = 200,
             previous: bool = False) -> str:
    try:
        return core_v1().read_namespaced_pod_log(
            name=pod, namespace=namespace, container=container, tail_lines=lines, previous=previous
        )
    except ApiException as e:
        if e.status == 400:  # e.g. container not started / no previous container
            return f"[no logs available: {e.reason}]"
        raise _api_error(f"reading logs for {pod}", e) from e
    except ClusterUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        reset_clients()
        raise _api_error(f"reading logs for {pod}", e) from e
