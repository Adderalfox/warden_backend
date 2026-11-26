"""Incident-response KPIs derived from the incident store (MTTR, success rate, stage timings...)."""
from __future__ import annotations

import statistics
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from app.graph.state import IncidentState

TERMINAL = {"COMPLETED", "FAILED"}


def _ts(entry: Dict[str, Any]) -> Optional[float]:
    try:
        return datetime.fromisoformat(entry["timestamp"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return None


def _mean(vals: Sequence[float]) -> Optional[float]:
    return statistics.fmean(vals) if vals else None


def _failure_stage(reason: Optional[str]) -> str:
    r = reason or ""
    head = r.split(":")[0]
    if ":" in r and head.endswith("Node"):
        return head
    if "rejected" in r.lower():
        return "Reviewer rejected"
    if "Tests failed" in r:
        return "Validation (tests)"
    return "Other"


def incident_summary(inc: IncidentState, created_at: Optional[float], now: float) -> Dict[str, Any]:
    logs = inc.get("agent_logs") or []
    last = _ts(logs[-1]) if logs else None
    terminal = inc["status"] in TERMINAL
    end = last if terminal and last else now
    return {
        "incident_id": inc["incident_id"],
        "service_name": inc["service_name"],
        "status": inc["status"],
        "error_summary": inc["error_summary"],
        "created_at": created_at,
        "duration_seconds": (end - created_at) if created_at else None,
        "failure_reason": inc.get("failure_reason"),
    }


def compute_incident_kpis(
    incidents: Sequence[IncidentState], created: Dict[str, float], now: Optional[float] = None
) -> Dict[str, Any]:
    now = now or time.time()
    total = len(incidents)
    completed = [i for i in incidents if i["status"] == "COMPLETED"]
    failed = [i for i in incidents if i["status"] == "FAILED"]
    awaiting = [i for i in incidents if i["status"] == "AWAITING_APPROVAL"]
    active = [i for i in incidents if i["status"] not in TERMINAL and i["status"] != "AWAITING_APPROVAL"]

    mttr: List[float] = []
    to_patch: List[float] = []
    approval_wait: List[float] = []
    stage: Dict[str, List[float]] = {}

    for inc in incidents:
        logs = inc.get("agent_logs") or []
        stamps = [_ts(e) for e in logs]
        t0 = created.get(inc["incident_id"])
        # Each log entry is written when its node finishes, so the gap to the previous entry is that node's duration.
        for i in range(1, len(logs)):
            if stamps[i] is not None and stamps[i - 1] is not None:
                stage.setdefault(logs[i]["node"], []).append(max(0.0, stamps[i] - stamps[i - 1]))
        if inc["status"] == "COMPLETED" and t0 and stamps and stamps[-1]:
            mttr.append(max(0.0, stamps[-1] - t0))
        if t0:
            for e, st in zip(logs, stamps):
                if e["node"] == "PatchGenerationNode" and st and "Generated" in e["summary"]:
                    to_patch.append(max(0.0, st - t0))
                    break
        wait_start = next((s for e, s in zip(logs, stamps) if e["node"] == "HumanApprovalNode" and s), None)
        wait_end = next((s for e, s in zip(logs, stamps) if e["node"] == "RemediationNode" and s), None)
        if wait_start and wait_end:
            approval_wait.append(max(0.0, wait_end - wait_start))

    validated = [i for i in incidents if (i.get("git_context") or {}).get("validation_result")]
    tests_ok = [i for i in validated if i.get("test_passed")]
    resolved = len(completed) + len(failed)

    reasons: Dict[str, int] = {}
    for i in failed:
        k = _failure_stage(i.get("failure_reason"))
        reasons[k] = reasons.get(k, 0) + 1

    hours = [0] * 24
    for iid, t0 in created.items():
        age_h = int((now - t0) // 3600)
        if 0 <= age_h < 24:
            hours[23 - age_h] += 1

    return {
        "total": total,
        "active": len(active),
        "awaiting_approval": len(awaiting),
        "completed": len(completed),
        "failed": len(failed),
        "success_rate": (len(completed) / resolved) if resolved else None,
        "mttr_seconds": _mean(mttr),
        "mttr_median_seconds": statistics.median(mttr) if mttr else None,
        "time_to_patch_seconds": _mean(to_patch),
        "approval_wait_seconds": _mean(approval_wait),
        "validation_pass_rate": (len(tests_ok) / len(validated)) if validated else None,
        "stage_avg_seconds": {k: _mean(v) for k, v in stage.items()},
        "failures_by_stage": reasons,
        "incidents_by_hour": hours,
    }
