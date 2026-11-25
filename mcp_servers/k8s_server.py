"""
mcp-k8s: standalone MCP server exposing real Kubernetes diagnostics tools.

Run standalone for testing with the MCP Inspector:
    mcp dev backend/mcp_servers/k8s_server.py

This module contains real `kubernetes` client calls -- no fixtures or fake
responses. If no kubeconfig/in-cluster config is reachable, tools raise a
clear, typed error explaining exactly what's missing, rather than silently
returning fabricated data.
"""
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel
from mcp.server.fastmcp import FastMCP

from app.core.config import settings

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.client.exceptions import ApiException
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "The 'kubernetes' package is required for mcp-k8s. Install backend deps first."
    ) from e


mcp = FastMCP("warden-mcp-k8s")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------
class PodStatus(BaseModel):
    pod_name: str
    phase: str
    ready: bool
    restart_count: int
    node_name: Optional[str] = None
    reason: Optional[str] = None
    message: Optional[str] = None


class PodStatusResult(BaseModel):
    namespace: str
    service_name: str
    pods: list[PodStatus]
    found: bool


class DeploymentDescription(BaseModel):
    name: str
    namespace: str
    replicas_desired: int
    replicas_available: int
    replicas_unavailable: int
    image: Optional[str]
    conditions: list[Dict[str, Any]]
    found: bool


# ---------------------------------------------------------------------------
# Client bootstrap (lazy, real)
# ---------------------------------------------------------------------------
_core_v1: Optional[k8s_client.CoreV1Api] = None
_apps_v1: Optional[k8s_client.AppsV1Api] = None


def _load_kube_config() -> None:
    if settings.kubeconfig_path:
        k8s_config.load_kube_config(config_file=settings.kubeconfig_path)
        return
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()  # falls back to ~/.kube/config


def _get_core_v1() -> k8s_client.CoreV1Api:
    global _core_v1
    if _core_v1 is None:
        _load_kube_config()
        _core_v1 = k8s_client.CoreV1Api()
    return _core_v1


def _get_apps_v1() -> k8s_client.AppsV1Api:
    global _apps_v1
    if _apps_v1 is None:
        _load_kube_config()
        _apps_v1 = k8s_client.AppsV1Api()
    return _apps_v1


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def get_pod_status(namespace: str, service_name: str) -> dict:
    """Get real-time pod status for all pods matching a service's label selector
    (tries `app=<service_name>` then `app.kubernetes.io/name=<service_name>`)."""
    core_v1 = _get_core_v1()
    pods = []
    for selector in (f"app={service_name}", f"app.kubernetes.io/name={service_name}"):
        try:
            resp = core_v1.list_namespaced_pod(namespace=namespace, label_selector=selector)
        except ApiException as e:
            raise RuntimeError(f"Kubernetes API error listing pods: {e.status} {e.reason}") from e
        if resp.items:
            for pod in resp.items:
                cs = pod.status.container_statuses or []
                restarts = sum(c.restart_count for c in cs) if cs else 0
                ready = all(c.ready for c in cs) if cs else False
                waiting = next((c.state.waiting for c in cs if c.state and c.state.waiting), None)
                pods.append(
                    PodStatus(
                        pod_name=pod.metadata.name,
                        phase=pod.status.phase or "Unknown",
                        ready=ready,
                        restart_count=restarts,
                        node_name=pod.spec.node_name,
                        reason=waiting.reason if waiting else None,
                        message=waiting.message if waiting else None,
                    )
                )
            break

    return PodStatusResult(
        namespace=namespace,
        service_name=service_name,
        pods=pods,
        found=len(pods) > 0,
    ).model_dump()


@mcp.tool()
def get_container_logs(pod_name: str, lines: int = 200, tail: bool = True) -> str:
    """Fetch real container logs for a pod via the Kubernetes API. Searches the
    configured default namespace for the pod."""
    core_v1 = _get_core_v1()
    try:
        return core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=settings.k8s_namespace_default,
            tail_lines=lines if tail else None,
        )
    except ApiException as e:
        raise RuntimeError(f"Kubernetes API error reading logs for {pod_name}: {e.status} {e.reason}") from e


@mcp.tool()
def describe_deployment(deployment_name: str, namespace: Optional[str] = None) -> dict:
    """Describe a real Deployment object: replica counts, image, and status conditions."""
    ns = namespace or settings.k8s_namespace_default
    apps_v1 = _get_apps_v1()
    try:
        dep = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=ns)
    except ApiException as e:
        if e.status == 404:
            return DeploymentDescription(
                name=deployment_name,
                namespace=ns,
                replicas_desired=0,
                replicas_available=0,
                replicas_unavailable=0,
                image=None,
                conditions=[],
                found=False,
            ).model_dump()
        raise RuntimeError(f"Kubernetes API error describing deployment: {e.status} {e.reason}") from e

    containers = dep.spec.template.spec.containers
    image = containers[0].image if containers else None
    conditions = [
        {
            "type": c.type,
            "status": c.status,
            "reason": c.reason,
            "message": c.message,
        }
        for c in (dep.status.conditions or [])
    ]

    return DeploymentDescription(
        name=deployment_name,
        namespace=ns,
        replicas_desired=dep.spec.replicas or 0,
        replicas_available=dep.status.available_replicas or 0,
        replicas_unavailable=dep.status.unavailable_replicas or 0,
        image=image,
        conditions=conditions,
        found=True,
    ).model_dump()


@mcp.tool()
async def get_pod_metrics(service_name: str, namespace: Optional[str] = None) -> dict:
    """Pod analytics for a service: live CPU/memory usage vs requests/limits, restarts,
    readiness, OOMKilled/CrashLoop state, plus rule-based health insights (memory
    pressure, CPU limit pressure, crash loops...). Same data as the Warden dashboard."""
    from app.metrics.service import MetricsError, metrics_service

    ns = namespace or settings.k8s_namespace_default
    try:
        return await metrics_service.diagnostic_snapshot(ns, service_name)
    except MetricsError as e:
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def get_pod_events(pod_name: str, namespace: Optional[str] = None) -> list:
    """Recent Kubernetes events for a pod (scheduling failures, OOMKilling, BackOff, probe failures)."""
    from app.metrics.service import MetricsError, metrics_service

    ns = namespace or settings.k8s_namespace_default
    try:
        return await metrics_service.events(ns, pod_name)
    except MetricsError as e:
        raise RuntimeError(str(e)) from e


if __name__ == "__main__":
    mcp.run()
