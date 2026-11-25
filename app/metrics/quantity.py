"""Kubernetes resource-quantity parsing (``"250m"``, ``"128Mi"``, ``"1500000n"``...)."""
from __future__ import annotations

import re
from typing import Optional, Union

_BIN = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DEC = {
    "n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1.0,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
}
_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[+-]?\.\d+)([A-Za-z]*)$")


def parse_quantity(q: Union[str, int, float, None]) -> float:
    """Parse a Kubernetes quantity into a plain float (cores / bytes)."""
    if q is None:
        raise ValueError("quantity is None")
    if isinstance(q, (int, float)):
        return float(q)
    m = _RE.match(str(q).strip())
    if not m:
        raise ValueError(f"unparseable quantity: {q!r}")
    number, suffix = float(m.group(1)), m.group(2)
    if suffix in _BIN:
        return number * _BIN[suffix]
    if suffix in _DEC:
        return number * _DEC[suffix]
    raise ValueError(f"unknown quantity suffix {suffix!r} in {q!r}")


def cpu_millicores(q: Union[str, int, float, None]) -> Optional[float]:
    return None if q is None else parse_quantity(q) * 1000.0


def mem_bytes(q: Union[str, int, float, None]) -> Optional[float]:
    return None if q is None else parse_quantity(q)
