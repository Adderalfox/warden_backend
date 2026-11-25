from app.metrics.models import ContainerSnapshot, PodSnapshot
from app.metrics.store import SampleStore


def _snap(name, cpu, mem, restarts=0):
    return PodSnapshot(namespace="ns", name=name, service="s", cpu_m=cpu, mem_b=mem, restart_count=restarts,
                       containers=[ContainerSnapshot(name="app", cpu_m=cpu, mem_b=mem)])


def test_bucketing_averages_and_alignment():
    store = SampleStore(retention_seconds=3600)
    base = 1_000_005  # aligned to the 15s grid so all three samples share one bucket
    for i, (cpu, rst) in enumerate([(100, 0), (200, 0), (300, 1)]):
        store.record("ns", [_snap("a", cpu, 1000 * (i + 1), rst)], base + i * 5)  # all inside one 15s bucket
    res = store.series("ns", ["a"], base - 30, base + 30, 15)
    assert all(t % 15 == 0 for t in res.timestamps)
    cpu = [v for v in res.metrics["cpu_m"]["a"] if v is not None]
    assert cpu == [200.0]                       # (100+200+300)/3
    assert [v for v in res.metrics["restarts"]["a"] if v is not None] == [1.0]  # max in bucket
    assert res.metrics["cpu_m"]["a"].count(None) == len(res.timestamps) - 1


def test_retention_prunes_and_unknown_pod_is_all_null():
    store = SampleStore(retention_seconds=60)
    store.record("ns", [_snap("a", 1, 1)], 1000)
    store.record("ns", [_snap("a", 2, 2)], 2000)       # first sample is now older than retention
    assert store.first_ts("ns", ["a"]) == 2000
    res = store.series("ns", ["ghost"], 1900, 2100, 15)
    assert set(res.metrics["cpu_m"]["ghost"]) == {None}


def test_container_series():
    store = SampleStore()
    store.record("ns", [_snap("a", 10, 100)], 1500)
    res = store.container_series("ns", "a", ["app"], 1440, 1560, 15)
    assert [v for v in res.metrics["mem_b"]["app"] if v is not None] == [100.0]
