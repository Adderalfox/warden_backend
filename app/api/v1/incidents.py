from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.core.store import store

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])


@router.get("")
async def list_incidents():
    return [dict(s) for s in store.list()]


@router.get("/{incident_id}")
async def get_incident(incident_id: str):
    state = store.get(incident_id)
    if state is None:
        raise HTTPException(404, "Incident not found")
    return dict(state)


@router.get("/{incident_id}/stream")
async def stream_incident(incident_id: str):
    state = store.get(incident_id)
    if state is None:
        raise HTTPException(404, "Incident not found")

    async def event_generator():
        queue = store.subscribe(incident_id)
        try:
            # Send current snapshot immediately so late subscribers aren't
            # stuck waiting for the next node to complete.
            current = store.get(incident_id)
            if current is not None:
                yield {"event": "snapshot", "data": json.dumps(dict(current))}

            while True:
                event = await queue.get()
                yield {"event": event["type"], "data": json.dumps(dict(event["state"]))}
                if event["type"] == "done":
                    break
        except asyncio.CancelledError:
            pass
        finally:
            store.unsubscribe(incident_id, queue)

    return EventSourceResponse(event_generator())
