from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .execution import RunProjection, utc_now


class Conversation(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    parent_conversation_id: UUID | None = None
    task: str
    created_at: datetime = Field(default_factory=utc_now)
    run: RunProjection = Field(default_factory=RunProjection)
