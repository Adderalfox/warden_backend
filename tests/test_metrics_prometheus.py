import httpx
import pytest

from app.metrics.prometheus import PrometheusClient, PrometheusError, pod_queries


def test_queries_escape_pod_names():
    q = pod_queries("prod", ["a.b-1", "c"], 15)
    assert 'namespace="prod"' in q["cpu_m"] and r"a\\.b\\-1|c" in q["cpu_m"]
    assert "container_memory_working_set_bytes" in q["mem_b"]
    assert "cfs_throttled_periods_total" in q["cpu_throttle_pct"]


def _transport(handler):
    return httpx.MockTransport(handler)


async def test_pod_series_grid_alignment_and_optional_metrics():
    start, step = 1000.0, 15
    def handler(req: httpx.Request) -> httpx.Response:
        query = req.url.params["query"]
        if "container_cpu_usage_seconds_total" in query:
            vals = [[start, "10"], [start + 15, "20"], [start + 45, "NaN"]]
        elif "container_memory_working_set_bytes" in query:
            vals = [[start + 30, "5000"]]
        else:  # restarts/net/throttle -> no data (e.g. kube-state-metrics not installed)
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix",
                                          "result": [{"metric": {"pod": "a"}, "values": vals}]}})

    prom = PrometheusClient("http://prom", transport=_transport(handler))
    res = await prom.pod_series("ns", ["a", "b"], start, start + 60, step)
    assert res.source == "prometheus" and len(res.timestamps) == 5
    assert res.metrics["cpu_m"]["a"] == [10.0, 20.0, None, None, None]      # NaN -> None
    assert res.metrics["mem_b"]["a"][2] == 5000.0
    assert set(res.metrics["cpu_m"]["b"]) == {None}                         # pod without data
    assert set(res.metrics["restarts"]["a"]) == {None}                      # optional metric absent


async def test_core_query_failure_raises():
    prom = PrometheusClient("http://prom", transport=_transport(lambda r: httpx.Response(500)))
    with pytest.raises(PrometheusError):
        await prom.pod_series("ns", ["a"], 1000.0, 1060.0, 15)
