"""Plain data shapes shared by the metrics sources, store, API and MCP tools."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ContainerSnapshot(BaseModel):
    name: str
    ready: bool = False
    restart_count: int = 0
    state: str = "unknown"                 # running | waiting | terminated | unknown
    reason: Optional[str] = None           # e.g. CrashLoopBackOff, OOMKilled
    message: Optional[str] = None
    last_termination_reason: Optional[str] = None
    last_termination_exit_code: Optional[int] = None
    last_termination_at: Optional[float] = None   # epoch seconds
    cpu_m: Optional[float] = None
    mem_b: Optional[float] = None
    cpu_req_m: Optional[float] = None
    cpu_lim_m: Optional[float] = None
    mem_req_b: Optional[float] = None
    mem_lim_b: Optional[float] = None


class PodSnapshot(BaseModel):
    namespace: str
    name: str
    service: str
    phase: str = "Unknown"
    ready: bool = False
    ready_containers: str = "0/0"
    restart_count: int = 0
    node_name: Optional[str] = None
    pod_ip: Optional[str] = None
    created_at: Optional[float] = None
    reason: Optional[str] = None           # most relevant waiting/terminated reason
    message: Optional[str] = None
    last_termination_reason: Optional[str] = None
    last_termination_at: Optional[float] = None
    cpu_m: Optional[float] = None          # None => no usage data (metrics-server missing)
    mem_b: Optional[float] = None
    cpu_req_m: Optional[float] = None
    cpu_lim_m: Optional[float] = None
    mem_req_b: Optional[float] = None
    mem_lim_b: Optional[float] = None
    containers: List[ContainerSnapshot] = Field(default_factory=list)
    labels: Dict[str, str] = Field(default_factory=dict)


class Insight(BaseModel):
    severity: str                          # critical | warning | info
    kind: str
    title: str
    detail: str
    pod: Optional[str] = None


class SeriesResult(BaseModel):
    """Grid-aligned multi-pod time series. ``None`` => no sample for that bucket."""
    start: float
    end: float
    step: int
    timestamps: List[float]
    pods: List[str]
    metrics: Dict[str, Dict[str, List[Optional[float]]]]   # metric -> pod -> values
    source: str = "collector"                              # collector | prometheus | demo
    history_seconds: float = 0.0                           # how much real history exists

    def as_dict(self) -> Dict[str, Any]:
        return self.model_dump()
