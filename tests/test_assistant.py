import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.assistant import gemini, tools
from app.core.config import settings
from app.metrics.digest import dashboard_digest


@pytest.fixture()
def demo(monkeypatch):
    monkeypatch.setattr(settings, "metrics_source", "demo")
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "gemini_model", "gemini-test")


def scripted(responses, seen):
    """Mock Gemini: returns the queued responses in order and records every request body."""
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["x-goog-api-key"] == "test-key"
        assert req.url.path.endswith("models/gemini-test:generateContent")
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=responses.pop(0))
    return httpx.MockTransport(handler)


def text(t):
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": t}]}}]}


def call(name, args, sig="SIG"):
    return {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}, "thoughtSignature": sig}]}}]}


async def test_tool_loop_runs_tool_and_returns_answer(demo, monkeypatch):
    seen = []
    monkeypatch.setattr(gemini, "_transport", lambda: scripted([
        call("get_pod_analytics", {"namespace": "demo", "service": "payments-api"}),
        text("payments-api-…h4v6r was OOMKilled; raise its memory limit."),
    ], seen))
    out = await gemini.chat([{"role": "user", "content": "why is payments restarting?"}],
                            {"page": "Pod Analytics", "namespace": "demo"})
    assert "OOMKilled" in out["reply"]
    assert out["tool_calls"] == [{"name": "get_pod_analytics", "args": {"namespace": "demo", "service": "payments-api"}, "ok": True}]
    first, second = seen
    assert first["tools"][0]["functionDeclarations"] and "Pod Analytics" in first["systemInstruction"]["parts"][0]["text"]
    # 2nd request carries the model's call verbatim (incl. thought signature) + our functionResponse
    assert second["contents"][-2]["parts"][0]["thoughtSignature"] == "SIG"
    fr = second["contents"][-1]["parts"][0]["functionResponse"]
    assert fr["name"] == "get_pod_analytics" and any(i["kind"] == "oom_killed" for i in fr["response"]["insights"])


async def test_tool_errors_are_reported_to_the_model_not_raised(demo, monkeypatch):
    seen = []
    monkeypatch.setattr(gemini, "_transport", lambda: scripted([
        call("get_pod_detail", {"namespace": "demo", "pod": "ghost"}), call("does_not_exist", {}), text("done"),
    ], seen))
    out = await gemini.chat([{"role": "user", "content": "x"}], {})
    assert out["reply"] == "done" and [c["ok"] for c in out["tool_calls"]] == [False, False]
    assert "not found" in json.dumps(seen[1]["contents"][-1])


async def test_tool_round_limit_forces_text_answer(demo, monkeypatch):
    monkeypatch.setattr(settings, "assistant_max_tool_rounds", 2)
    seen = []
    monkeypatch.setattr(gemini, "_transport", lambda: scripted(
        [call("list_namespaces", {}), call("list_namespaces", {}), text("final")], seen))
    out = await gemini.chat([{"role": "user", "content": "x"}], {})
    assert out["reply"] == "final" and "toolConfig" not in seen[1]
    assert seen[2]["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"


async def test_history_mapping_and_api_errors(demo, monkeypatch):
    contents = gemini._history_to_contents([
        {"role": "assistant", "content": "hello"}, {"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    assert [c["role"] for c in contents] == ["user", "model", "user"] and contents[2]["parts"][0]["text"] == "a\n\nb"

    def handler(req):
        return httpx.Response(400, json={"error": {"message": "API key not valid"}})
    monkeypatch.setattr(gemini, "_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(gemini.AssistantError, match="API key not valid"):
        await gemini.chat([{"role": "user", "content": "x"}], {})
    monkeypatch.setattr(gemini, "_transport", lambda: httpx.MockTransport(lambda r: httpx.Response(429, json={})))
    with pytest.raises(gemini.AssistantError) as e:
        await gemini.chat([{"role": "user", "content": "x"}], {})
    assert e.value.status == 429


async def test_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", None)
    with pytest.raises(gemini.AssistantError) as e:
        await gemini.chat([{"role": "user", "content": "x"}], {})
    assert e.value.status == 503 and "WARDEN_GEMINI_API_KEY" in str(e.value)


async def test_every_tool_runs_against_demo_data(demo):
    inc_free = {
        "list_namespaces": {}, "list_services": {"namespace": "demo"},
        "get_pod_analytics": {"namespace": "demo", "range": "15m"},
        "get_pod_detail": {"namespace": "demo", "pod": "payments-api-5b7c8d9f4-h4v6r"},
        "get_pod_events": {"namespace": "demo", "pod": "payments-api-5b7c8d9f4-h4v6r"},
        "get_pod_logs": {"namespace": "demo", "pod": "search-indexer-7f6c5d8b9-k3j9x", "lines": 5},
        "get_performance_report": {"namespace": "demo"}, "list_incidents": {}, "get_incident": {"incident_id": "nope"},
    }
    assert set(inc_free) == set(tools.EXECUTORS) == {d["name"] for d in tools.DECLARATIONS}
    for name, args in inc_free.items():
        res = await tools.execute(name, args)
        assert isinstance(res, dict), name
        if name != "get_incident":
            assert "error" not in res, (name, res)
    assert (await tools.execute("get_pod_analytics", {"namespace": "demo", "range": "bogus"}))["error"]
    assert "error" in await tools.execute("get_pod_analytics", {})            # missing required arg


async def test_digest_is_compact(demo, monkeypatch):
    from types import SimpleNamespace
    from app.metrics import demo as demo_src
    from app.metrics.service import metrics_service
    # Freeze time 25 min into the leak pod's sawtooth (and well after the demo's restart anchor),
    # so the result doesn't depend on when the test runs.
    base = demo_src.R0 + 7200
    fixed = base + ((1500 - (base - demo_src.T0 + 4 * 211)) % 2400)
    monkeypatch.setattr("app.metrics.service.time", SimpleNamespace(time=lambda: float(fixed)))
    dash = await metrics_service.dashboard("demo", None, 3600)
    d = dashboard_digest(dash)
    assert len(json.dumps(d)) < 20_000 and "timestamps" not in json.dumps(d)
    leak = d["series_stats_per_pod"]["payments-api-5b7c8d9f4-h4v6r"]
    assert leak["mem_trend_mib_per_min"] > 0 and leak["mem_mib"]["max"] > leak["mem_mib"]["min"]


def test_chat_endpoint_validation_and_errors(demo, monkeypatch):
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/assistant/status").json() == {"configured": True, "model": "gemini-test"}
        assert c.post("/api/v1/assistant/chat", json={"messages": []}).status_code == 422
        assert c.post("/api/v1/assistant/chat", json={"messages": [{"role": "system", "content": "x"}]}).status_code == 422
        monkeypatch.setattr(gemini, "_transport", lambda: scripted([text("hi there")], []))
        r = c.post("/api/v1/assistant/chat", json={"messages": [{"role": "user", "content": "hello"}], "context": {"page": "Overview"}})
        assert r.status_code == 200 and r.json()["reply"] == "hi there"
        monkeypatch.setattr(settings, "gemini_api_key", None)
        assert c.post("/api/v1/assistant/chat", json={"messages": [{"role": "user", "content": "x"}]}).status_code == 503
