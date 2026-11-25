import time

from app.metrics.insights import compute_insights, linear_trend, summarize
from app.metrics.models import ContainerSnapshot, PodSnapshot, SeriesResult

MIB = 2**20


def _pod(**kw) -> PodSnapshot:
    base = dict(namespace="ns", name="p1", service="svc", phase="Running", ready=True, ready_containers="1/1",
                mem_b=100 * MIB, mem_lim_b=512 * MIB, cpu_m=50.0, cpu_lim_m=500.0, containers=[])
    base.update(kw)
    return PodSnapshot(**base)


def kinds(insights):
    return {i.kind for i in insights}


def test_healthy_pod_has_no_alerts():
    assert compute_insights([_pod()]) == []


def test_oom_and_memory_pressure():
    now = time.time()
    ins = compute_insights([_pod(mem_b=480 * MIB, last_termination_reason="OOMKilled", last_termination_at=now - 120)], now=now)
    assert {"oom_killed", "memory_pressure"} <= kinds(ins)
    assert ins[0].severity == "critical"


def test_crashloop_detected_from_container_state():
    c = ContainerSnapshot(name="app", state="waiting", reason="CrashLoopBackOff", restart_count=9,
                          last_termination_reason="Error", last_termination_exit_code=1)
    ins = compute_insights([_pod(ready=False, ready_containers="0/1", containers=[c])])
    assert "crashloopbackoff" in kinds(ins)
    assert "not_ready" not in kinds(ins)  # already explained by the crash loop


def test_cpu_pressure_and_no_limit_info():
    ins = compute_insights([_pod(cpu_m=480.0), _pod(name="p2", mem_lim_b=None)])
    assert "cpu_pressure" in kinds(ins) and "no_memory_limit" in kinds(ins)


def test_pending_too_long():
    now = time.time()
    ins = compute_insights([_pod(phase="Pending", ready=False, created_at=now - 900, mem_b=None, cpu_m=None)], now=now)
    assert "pending" in kinds(ins)


def test_linear_trend_requires_enough_data():
    assert linear_trend([0, 15, 30], [1, 2, 3]) is None
    t = [i * 15.0 for i in range(40)]
    fit = linear_trend(t, [10 + 2 * x for x in t])
    assert fit and abs(fit["slope"] - 2) < 1e-6 and fit["r2"] > 0.99


def test_memory_leak_eta_from_series():
    step = 15
    ts = [1000.0 + i * step for i in range(60)]                 # 15 minutes
    growth = 0.4 * MIB                                           # per 15s => ~1.6 MiB/min
    mem = [200 * MIB + i * growth for i in range(60)]
    mem_now = mem[-1]
    lim = mem_now + 10 * MIB                                     # ~6 min of headroom at ~1.6 MiB/min
    series = SeriesResult(start=ts[0], end=ts[-1], step=step, timestamps=ts, pods=["p1"],
                          metrics={"mem_b": {"p1": mem}, "restarts": {"p1": [0.0] * 60}})
    ins = compute_insights([_pod(mem_b=mem_now, mem_lim_b=lim)], series)
    leak = [i for i in ins if i.kind == "memory_leak"]
    assert leak and leak[0].severity == "critical"


def test_restarts_in_window_and_summary():
    ts = [0.0, 15.0, 30.0]
    series = SeriesResult(start=0, end=30, step=15, timestamps=ts, pods=["p1"],
                          metrics={"restarts": {"p1": [2.0, 2.0, 4.0]}, "mem_b": {"p1": [None] * 3}})
    pods = [_pod(restart_count=4), _pod(name="p2", phase="Pending", ready=False, cpu_m=None, mem_b=None)]
    assert "restarts_in_window" in kinds(compute_insights(pods, series))
    s = summarize(pods, series)
    assert s["pods_total"] == 2 and s["running"] == 1 and s["pending"] == 1
    assert s["restarts_in_window"] == 2 and s["cpu_m_total"] == 50.0
