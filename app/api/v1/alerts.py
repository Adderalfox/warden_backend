from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Header, Request, status
from pydantic import BaseModel, Field

from app.core.security import verify_webhook_signature
from app.core.store import store
from app.graph.runner import run_incident
from app.graph.state import IncidentState

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


class IncidentEvent(BaseModel):
    """Normalized shape we accept from Alertmanager, Sentry, or a generic
    JSON payload. `source_format` selects which of the loosely-typed inputs
    below to read."""

    service_name: str
    error_summary: str
    raw_stack_trace: str = ""
    repo_url: str
    namespace: Optional[str] = None
    upstream_alert_id: Optional[str] = Field(
        default=None, description="Upstream fingerprint/ID used for idempotency dedupe if present"
    )


def _normalize_payload(raw: Dict[str, Any]) -> IncidentEvent:
    """Real (not stubbed) normalization across the three supported shapes."""
    # Prometheus Alertmanager shape: {"alerts": [{"labels": {...}, "annotations": {...}}]}
    if "alerts" in raw and isinstance(raw["alerts"], list) and raw["alerts"]:
        alert = raw["alerts"][0]
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        return IncidentEvent(
            service_name=labels.get("service", labels.get("job", "unknown-service")),
            error_summary=annotations.get("summary", "Alertmanager alert"),
            raw_stack_trace=annotations.get("description", ""),
            repo_url=labels.get("repo_url", raw.get("repo_url", "")),
            namespace=labels.get("namespace"),
            upstream_alert_id=alert.get("fingerprint"),
        )

    # Sentry shape: {"event": {"culprit": ..., "message": ..., "exception": {...}}}
    if "event" in raw and isinstance(raw["event"], dict):
        event = raw["event"]
        exc = (event.get("exception") or {}).get("values", [{}])
        stacktrace = ""
        if exc and exc[0].get("stacktrace"):
            frames = exc[0]["stacktrace"].get("frames", [])
            stacktrace = "\n".join(
                f'  File "{f.get("filename")}", line {f.get("lineno")}, in {f.get("function")}'
                for f in frames
            )
        return IncidentEvent(
            service_name=event.get("server_name", raw.get("project", "unknown-service")),
            error_summary=event.get("message") or event.get("culprit") or "Sentry event",
            raw_stack_trace=stacktrace,
            repo_url=raw.get("repo_url", ""),
            namespace=raw.get("namespace"),
            upstream_alert_id=event.get("event_id"),
        )

    # Generic JSON shape: already close to IncidentEvent.
    return IncidentEvent(**raw)


@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_signature: Optional[str] = Header(default=None),
):
    raw_body = await request.body()
    verify_webhook_signature(raw_body, x_signature)

    raw_json = await request.json()
    event = _normalize_payload(raw_json)

    dedupe_key = event.upstream_alert_id or hashlib.sha256(raw_body).hexdigest()
    if store.seen_recently(dedupe_key):
        return {"status": "duplicate_ignored", "dedupe_key": dedupe_key}

    incident_id = f"inc-{uuid.uuid4().hex[:12]}"
    initial_state: IncidentState = {
        "incident_id": incident_id,
        "service_name": event.service_name,
        "status": "INGESTED",
        "error_summary": event.error_summary,
        "raw_stack_trace": event.raw_stack_trace,
        "logs_collected": [],
        "git_context": {},
        "proposed_diff": None,
        "test_passed": False,
        "pr_url": None,
        "agent_logs": [],
        "repo_url": event.repo_url,
        "repo_path": "",
        "affected_files": [],
        "namespace": event.namespace or "default",
        "approval_decision": None,
        "failure_reason": None,
    }
    store.put(initial_state)

    background_tasks.add_task(run_incident, initial_state)

    return {"status": "accepted", "incident_id": incident_id}
