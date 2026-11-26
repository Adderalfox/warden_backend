from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.assistant import gemini
from app.core.config import settings

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class PageContext(BaseModel):
    page: Optional[str] = Field(None, max_length=80)
    path: Optional[str] = Field(None, max_length=200)
    namespace: Optional[str] = Field(None, max_length=80)
    service: Optional[str] = Field(None, max_length=120)
    range: Optional[str] = Field(None, max_length=10)
    pod: Optional[str] = Field(None, max_length=200)
    incident_id: Optional[str] = Field(None, max_length=40)


class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(min_length=1, max_length=60)
    context: PageContext = PageContext()


@router.get("/status")
async def status():
    return {"configured": gemini.configured(), "model": settings.gemini_model}


@router.post("/chat")
async def chat(body: ChatRequest) -> Dict[str, Any]:
    try:
        return await gemini.chat([m.model_dump() for m in body.messages], body.context.model_dump())
    except gemini.AssistantError as e:
        raise HTTPException(e.status, str(e)) from e
