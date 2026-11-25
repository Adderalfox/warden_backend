import pytest

from app.metrics.quantity import cpu_millicores, mem_bytes, parse_quantity


@pytest.mark.parametrize(
    "raw,expected",
    [("250m", 250.0), ("2", 2000.0), ("1500000n", 1.5), ("500000u", 500.0), ("0", 0.0)],
)
def test_cpu_millicores(raw, expected):
    assert cpu_millicores(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw,expected",
    [("128Mi", 128 * 2**20), ("12345Ki", 12345 * 1024), ("1Gi", 2**30), ("1G", 1e9), ("1.5Gi", 1.5 * 2**30), ("123456", 123456)],
)
def test_mem_bytes(raw, expected):
    assert mem_bytes(raw) == pytest.approx(expected)


def test_exponent_and_errors():
    assert parse_quantity("1e3") == 1000
    assert cpu_millicores(None) is None
    with pytest.raises(ValueError):
        parse_quantity("12Zz")
    with pytest.raises(ValueError):
        parse_quantity("abc")
