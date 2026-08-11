from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .execution import utc_now
from .project import ProjectConfig


class Session(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    tenant_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    config: ProjectConfig | None = None
    lease_epoch: int = 0
    active_container_id: str | None = None
    active_run_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
