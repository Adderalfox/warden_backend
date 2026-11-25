"""End-to-end API test against the opt-in synthetic source (no cluster needed)."""
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "metrics_source", "demo")
    monkeypatch.setattr(settings, "prometheus_url", None)
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_status_and_discovery(client):
    st = client.get("/api/v1/metrics/status").json()
    assert st["synthetic"] is True and st["series_source"] == "demo"
    assert client.get("/api/v1/metrics/namespaces").json()["namespaces"] == ["demo"]
    assert "payments-api" in client.get("/api/v1/metrics/services?namespace=demo").json()["services"]


def test_dashboard_shape_and_flags(client):
    d = client.get("/api/v1/metrics/dashboard?namespace=demo&range=1h").json()
    assert d["source"]["synthetic"] is True
    assert d["summary"]["pods_total"] == 10
    s = d["series"]
    n = len(s["timestamps"])
    assert n > 50 and all(len(v) == n for v in s["metrics"]["cpu_m"].values())
    assert set(s["metrics"]) >= {"cpu_m", "mem_b", "restarts", "net_rx_bps", "cpu_throttle_pct"}
    assert {"oom_killed", "crashloopbackoff"} <= {i["kind"] for i in d["insights"]}
    assert d["insights"][0]["severity"] == "critical"


def test_dashboard_service_filter_and_bad_range(client):
    d = client.get("/api/v1/metrics/dashboard?namespace=demo&service=checkout-service&range=15m").json()
    assert d["summary"]["pods_total"] == 3 and {p["service"] for p in d["pods"]} == {"checkout-service"}
    assert client.get("/api/v1/metrics/dashboard?namespace=demo&range=banana").status_code == 422


def test_pod_detail_events_logs(client):
    pod = "payments-api-5b7c8d9f4-h4v6r"
    d = client.get(f"/api/v1/metrics/pods/demo/{pod}?range=30m").json()
    assert d["pod"]["last_termination_reason"] == "OOMKilled"
    assert "app" in d["container_series"]["metrics"]["mem_b"]
    ev = client.get(f"/api/v1/metrics/pods/demo/{pod}/events").json()["events"]
    assert any(e["reason"] == "OOMKilling" for e in ev)
    assert "DEMO DATA" in client.get(f"/api/v1/metrics/pods/demo/{pod}/logs?lines=5").text
    assert client.get("/api/v1/metrics/pods/demo/nope").status_code == 404


def test_diagnostic_snapshot_for_agent(client):
    import asyncio
    from app.metrics.service import metrics_service
    snap = asyncio.run(metrics_service.diagnostic_snapshot("demo", "payments-api"))
    assert snap["found"] and snap["synthetic"] and len(snap["pods"]) == 2
    assert any(i["kind"] == "oom_killed" for i in snap["insights"])
