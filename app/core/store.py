"""
Process-wide incident registry: current IncidentState snapshots, per-incident
SSE subscriber queues, and a dedupe cache for webhook idempotency. In-memory
by design for this phase (LangGraph's MemorySaver checkpointer is likewise
in-process) -- swap for Redis/Postgres without touching the API layer if you
need multi-process durability.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

from app.graph.state import IncidentState


class IncidentStore:
    def __init__(self) -> None:
        self._incidents: Dict[str, IncidentState] = {}
        self._created: Dict[str, float] = {}
        self._subscribers: Dict[str, list[asyncio.Queue]] = {}
        self._dedupe_cache: Dict[str, float] = {}
        self._dedupe_window_seconds = 300

    # --- CRUD ---
    def put(self, state: IncidentState) -> None:
        self._created.setdefault(state["incident_id"], time.time())
        self._incidents[state["incident_id"]] = state

    def created_at(self, incident_id: str) -> Optional[float]:
        return self._created.get(incident_id)

    def get(self, incident_id: str) -> Optional[IncidentState]:
        return self._incidents.get(incident_id)

    def list(self) -> list[IncidentState]:
        # newest first (incident ids are random, so sort by when we first saw them)
        return sorted(
            self._incidents.values(),
            key=lambda s: self._created.get(s["incident_id"], 0.0),
            reverse=True,
        )

    # --- Idempotency ---
    def seen_recently(self, content_hash: str) -> bool:
        now = time.time()
        self._dedupe_cache = {
            k: v for k, v in self._dedupe_cache.items() if now - v < self._dedupe_window_seconds
        }
        if content_hash in self._dedupe_cache:
            return True
        self._dedupe_cache[content_hash] = now
        return False

    # --- Pub/sub for SSE ---
    def subscribe(self, incident_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(incident_id, []).append(q)
        return q

    def unsubscribe(self, incident_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(incident_id, [])
        if q in subs:
            subs.remove(q)

    async def publish(self, incident_id: str, event: dict) -> None:
        for q in list(self._subscribers.get(incident_id, [])):
            await q.put(event)


store = IncidentStore()
