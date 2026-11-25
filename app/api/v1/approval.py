from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.core.security import require_approval_api_key
from app.core.store import store
from app.graph.runner import resume_incident

router = APIRouter(prefix="/api/v1/approval", tags=["approval"])


class ApprovalDecision(BaseModel):
    decision: Literal["APPROVE", "REJECT"]


@router.post("/{incident_id}", dependencies=[Depends(require_approval_api_key)])
async def submit_approval(incident_id: str, payload: ApprovalDecision, background_tasks: BackgroundTasks):
    state = store.get(incident_id)
    if state is None:
        raise HTTPException(404, "Incident not found")
    if state["status"] != "AWAITING_APPROVAL":
        raise HTTPException(409, f"Incident is not awaiting approval (status={state['status']})")

    background_tasks.add_task(resume_incident, incident_id, payload.decision)
    return {"status": "resuming", "incident_id": incident_id, "decision": payload.decision}
