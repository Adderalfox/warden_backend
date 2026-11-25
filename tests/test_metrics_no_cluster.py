"""Default (real) mode with an unreachable cluster must fail loudly and clearly --
never fabricate pods -- and must not crash the app or the collector."""
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.metrics import k8s_source


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "metrics_source", "kubernetes")
    monkeypatch.setattr(settings, "kubeconfig_path", "/definitely/not/a/kubeconfig")
    monkeypatch.setattr(settings, "prometheus_url", None)
    k8s_source.reset_clients()
    from app.main import app
    with TestClient(app) as c:
        yield c
    k8s_source.reset_clients()


def test_dashboard_returns_clear_503(client):
    r = client.get("/api/v1/metrics/dashboard?namespace=default")
    assert r.status_code == 503
    assert "Kubernetes configuration" in r.json()["detail"]


def test_status_still_works_and_reports_not_synthetic(client):
    st = client.get("/api/v1/metrics/status").json()
    assert st["synthetic"] is False and st["source"] == "kubernetes"


def test_namespaces_503(client):
    assert client.get("/api/v1/metrics/namespaces").status_code == 503
