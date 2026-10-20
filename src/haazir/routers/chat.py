"""`POST /chat`. The agent is in `services/chat.py`."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..auth.deps import Ctx
from ..services import chat

router = APIRouter(prefix="/v1", tags=["chat"])


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "model"]
    content: str = Field(min_length=1, max_length=2000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    area: str | None = Field(default=None, max_length=60)
    cards: list[str] = Field(default_factory=list, max_length=8)


@router.post("/chat")
async def chat_turn(body: ChatIn, ctx: Ctx) -> dict:
    messages = [m.model_dump() for m in body.messages]
    if messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": "?"})
    return await chat.respond(
        ctx, messages, lat=body.lat, lng=body.lng, area=body.area, cards=body.cards
    )
