import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.analytics.efficiency import health_score, per_service
from app.analytics.incidents import compute_incident_kpis
from app.core.config import settings
from app.core.store import store
from app.metrics import demo
from app.metrics.models import Insight

NOW = 1_800_000_000.0


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def inc(iid, status, steps, failure=None, test_passed=False, validated=False):
    return {
        "incident_id": iid, "service_name": "svc", "status": status, "error_summary": "boom", "raw_stack_trace": "",
        "logs_collected": [], "git_context": {"validation_result": {"passed": test_passed}} if validated else {},
        "proposed_diff": None, "test_passed": test_passed, "pr_url": None,
        "agent_logs": [{"node": n, "timestamp": iso(t), "summary": s, "tool_calls": []} for n, t, s in steps],
        "repo_url": "", "repo_path": "", "affected_files": [], "namespace": "default",
        "approval_decision": None, "failure_reason": failure,
    }


def test_incident_kpis():
    t0 = NOW - 600
    done = inc("a", "COMPLETED", [
        ("IngestNode", t0 + 2, "Parsed"), ("DiagnosticNode", t0 + 10, "Collected"),
        ("PatchGenerationNode", t0 + 40, "Generated candidate patch"), ("ValidationNode", t0 + 70, "ok"),
        ("HumanApprovalNode", t0 + 71, "Waiting"), ("RemediationNode", t0 + 131, "Opened PR"),
    ], test_passed=True, validated=True)
    bad = inc("b", "FAILED", [("IngestNode", t0, "x"), ("PatchGenerationNode", t0 + 5, "FAILED: boom")],
              failure="PatchGenerationNode: RuntimeError('no key')", validated=True)
    waiting = inc("c", "AWAITING_APPROVAL", [("IngestNode", t0, "x")])
    running = inc("d", "DIAGNOSING", [])
    created = {"a": t0, "b": t0, "c": t0, "d": NOW - 60}
    k = compute_incident_kpis([done, bad, waiting, running], created, now=NOW)
    assert (k["total"], k["completed"], k["failed"], k["awaiting_approval"], k["active"]) == (4, 1, 1, 1, 1)
    assert k["success_rate"] == 0.5
    assert k["mttr_seconds"] == pytest.approx(131)
    assert k["time_to_patch_seconds"] == pytest.approx(40)
    assert k["approval_wait_seconds"] == pytest.approx(60)
    assert k["validation_pass_rate"] == 0.5
    assert k["failures_by_stage"] == {"PatchGenerationNode": 1}
    assert k["stage_avg_seconds"]["PatchGenerationNode"] == pytest.approx((30 + 5) / 2)
    assert sum(k["incidents_by_hour"]) == 4 and len(k["incidents_by_hour"]) == 24


def test_empty_incident_kpis_have_no_fake_numbers():
    k = compute_incident_kpis([], {}, now=NOW)
    assert k["total"] == 0 and k["success_rate"] is None and k["mttr_seconds"] is None


def test_health_score_breakdown_and_flags():
    pods = demo.snapshot("demo")
    crit = [Insight(severity="critical", kind="x", title="t", detail="d")] * 2
    warn = [Insight(severity="warning", kind="y", title="t", detail="d")] * 3
    h = health_score(pods, crit + warn)
    assert h["score"] == 100 - 30 - 12 - round(15 * 3 / 10) and h["grade"] == "critical"     # 54 < 60
    mild = health_score(pods, crit[:1] + warn[:1])                                           # 100-15-4-4 = 77
    assert mild["score"] == 77 and mild["grade"] == "degraded"
    assert [p["points"] for p in mild["penalties"]] == [15, 4, 4]
    assert health_score([], [])["score"] is None
    rows = {r["service"]: r for r in per_service(pods)}
    assert rows["payments-api"]["restarts"] > 0 and rows["frontend-web"]["availability_pct"] == 100.0
    assert rows["notifier"]["availability_pct"] == 0.0


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "metrics_source", "demo")
    store._incidents.clear(); store._created.clear()
    from app.main import app
    with TestClient(app) as c:
        yield c
    store._incidents.clear(); store._created.clear()


def test_overview_and_performance_endpoints(client):
    store.put(inc("inc-1", "COMPLETED", [("IngestNode", time.time() - 30, "x"), ("RemediationNode", time.time() - 5, "PR")]))
    o = client.get("/api/v1/analytics/overview?namespace=demo").json()
    assert o["incidents"]["kpis"]["total"] == 1 and o["incidents"]["recent"][0]["incident_id"] == "inc-1"
    assert o["cluster"]["error"] is None and o["cluster"]["synthetic"] is True
    assert o["cluster"]["health"]["score"] is not None and len(o["cluster"]["services"]) == 6
    p = client.get("/api/v1/analytics/performance?namespace=demo").json()
    assert "totals" in p["cluster"] and p["incidents"]["kpis"]["mttr_seconds"] is not None


def test_overview_survives_unreachable_cluster(client, monkeypatch):
    from app.metrics import k8s_source
    monkeypatch.setattr(settings, "metrics_source", "kubernetes")
    monkeypatch.setattr(settings, "kubeconfig_path", "/no/such/kubeconfig")
    k8s_source.reset_clients()
    o = client.get("/api/v1/analytics/overview?namespace=default").json()
    assert o["cluster"]["error"] and "Kubernetes configuration" in o["cluster"]["error"]
    assert "kpis" in o["incidents"]                                       # incident side still works
    k8s_source.reset_clients()


def test_incident_list_is_newest_first_with_created_at(client):
    store.put(inc("old", "COMPLETED", []))
    time.sleep(0.01)
    store.put(inc("new", "DIAGNOSING", []))
    rows = client.get("/api/v1/incidents").json()
    assert [r["incident_id"] for r in rows] == ["new", "old"] and rows[0]["created_at"] > rows[1]["created_at"]
