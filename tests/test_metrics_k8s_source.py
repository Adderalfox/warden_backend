"""Builds real `kubernetes` client model objects (what the API returns) and
checks pod -> PodSnapshot conversion, incl. OOMKilled + CrashLoopBackOff."""
from datetime import datetime, timedelta, timezone

from kubernetes import client as k

from app.metrics import k8s_source


def _pod(name="payments-abc", restarts=3, last_reason="OOMKilled", waiting=None, ready=False):
    now = datetime.now(timezone.utc)
    state = k.V1ContainerState(waiting=k.V1ContainerStateWaiting(reason=waiting, message="back-off")) if waiting \
        else k.V1ContainerState(running=k.V1ContainerStateRunning(started_at=now))
    return k.V1Pod(
        metadata=k.V1ObjectMeta(name=name, namespace="prod", labels={"app": "payments"},
                                creation_timestamp=now - timedelta(hours=2)),
        spec=k.V1PodSpec(
            node_name="node-1",
            containers=[k.V1Container(
                name="app",
                resources=k.V1ResourceRequirements(
                    requests={"cpu": "200m", "memory": "256Mi"}, limits={"cpu": "500m", "memory": "512Mi"}),
            )],
        ),
        status=k.V1PodStatus(
            phase="Running", pod_ip="10.0.0.5",
            container_statuses=[k.V1ContainerStatus(
                name="app", ready=ready, restart_count=restarts, image="x", image_id="y", state=state,
                last_state=k.V1ContainerState(terminated=k.V1ContainerStateTerminated(
                    exit_code=137, reason=last_reason, finished_at=now - timedelta(minutes=5))),
            )],
        ),
    )


def test_oomkilled_pod_snapshot_with_usage():
    snap = k8s_source.pod_to_snapshot(_pod(ready=True), {"app": (320.0, 480 * 2**20)})
    assert snap.service == "payments" and snap.namespace == "prod"
    assert snap.restart_count == 3 and snap.ready and snap.ready_containers == "1/1"
    assert snap.last_termination_reason == "OOMKilled"
    assert snap.cpu_m == 320.0 and snap.mem_b == 480 * 2**20
    assert snap.cpu_lim_m == 500.0 and snap.mem_lim_b == 512 * 2**20
    assert snap.mem_req_b == 256 * 2**20 and snap.cpu_req_m == 200.0


def test_crashloop_pod_without_metrics():
    snap = k8s_source.pod_to_snapshot(_pod(waiting="CrashLoopBackOff"), None)
    assert snap.reason == "CrashLoopBackOff" and not snap.ready
    assert snap.cpu_m is None and snap.mem_b is None  # no fabricated usage
    assert snap.containers[0].state == "waiting"


def test_partial_limits_are_not_summed():
    pod = _pod()
    pod.spec.containers.append(k.V1Container(name="sidecar"))  # no resources at all
    snap = k8s_source.pod_to_snapshot(pod, None)
    assert snap.mem_lim_b is None and snap.cpu_lim_m is None
