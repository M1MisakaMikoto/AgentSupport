from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class OutboxNotification(BaseModel):
    id: UUID
    topic: str
    aggregate_id: UUID
    payload: dict
    attempts: int
    created_at: datetime
