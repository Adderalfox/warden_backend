from typing import Any, Dict, List, Optional, TypedDict


class AgentLogEntry(TypedDict):
    node: str
    timestamp: str
    summary: str
    tool_calls: List[str]


class IncidentState(TypedDict):
    incident_id: str
    service_name: str
    status: str  # INGESTED, DIAGNOSING, PATCH_GENERATED, AWAITING_APPROVAL, COMPLETED, FAILED
    error_summary: str
    raw_stack_trace: str
    logs_collected: List[str]
    git_context: Dict[str, Any]
    proposed_diff: Optional[str]
    test_passed: bool
    pr_url: Optional[str]
    agent_logs: List[AgentLogEntry]

    # Fields carried alongside the fixed contract for internal node use.
    repo_url: str
    repo_path: str
    affected_files: List[str]
    namespace: str
    approval_decision: Optional[str]  # APPROVE | REJECT, set by the API layer
    failure_reason: Optional[str]


VALID_TRANSITIONS: Dict[str, List[str]] = {
    "INGESTED": ["DIAGNOSING", "FAILED"],
    "DIAGNOSING": ["PATCH_GENERATED", "FAILED"],
    "PATCH_GENERATED": ["AWAITING_APPROVAL", "FAILED"],
    "AWAITING_APPROVAL": ["COMPLETED", "FAILED"],
    "COMPLETED": [],
    "FAILED": [],
}


def can_transition(current: str, target: str) -> bool:
    return target in VALID_TRANSITIONS.get(current, [])
