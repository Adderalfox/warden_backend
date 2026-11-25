"""
Drives the compiled LangGraph, streaming each node's resulting state into the
IncidentStore (and out over SSE) as it happens, and stopping cleanly at the
AWAITING_APPROVAL interrupt. `resume_incident` continues a paused graph after
a human approval/rejection decision.
"""
from __future__ import annotations

from langgraph.types import Command

from app.core.store import store
from app.graph.state import IncidentState
from app.graph.workflow import warden_graph


def _thread_config(incident_id: str) -> dict:
    return {"configurable": {"thread_id": incident_id}}


async def run_incident(initial_state: IncidentState) -> None:
    config = _thread_config(initial_state["incident_id"])
    store.put(initial_state)
    await store.publish(initial_state["incident_id"], {"type": "snapshot", "state": initial_state})

    async for event in warden_graph.astream(initial_state, config=config, stream_mode="values"):
        state: IncidentState = event  # type: ignore[assignment]
        store.put(state)
        await store.publish(state["incident_id"], {"type": "snapshot", "state": state})

    final = store.get(initial_state["incident_id"])
    if final is not None:
        await store.publish(final["incident_id"], {"type": "done", "state": final})


async def resume_incident(incident_id: str, decision: str) -> None:
    config = _thread_config(incident_id)

    async for event in warden_graph.astream(Command(resume=decision), config=config, stream_mode="values"):
        state: IncidentState = event  # type: ignore[assignment]
        store.put(state)
        await store.publish(incident_id, {"type": "snapshot", "state": state})

    final = store.get(incident_id)
    if final is not None:
        await store.publish(incident_id, {"type": "done", "state": final})
