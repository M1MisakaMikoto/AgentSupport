from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .execution import utc_now


class Session(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    lease_epoch: int = 0
    active_container_id: str | None = None
    active_run_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
