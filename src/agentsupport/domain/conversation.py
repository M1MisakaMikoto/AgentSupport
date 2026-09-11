from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .execution import RunProjection, utc_now


class ConversationMode(StrEnum):
    """Conversation-level execution mode."""

    DEFAULT = "default"
    #: 无审批：全部工具可用（含 bash），无工作区限制。
    NO_APPROVAL = "no_approval"
    #: 静默模式：仅工作区内工具、路径受限，无需审批（供静默任务使用）。
    SILENT = "silent"


class Conversation(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    parent_conversation_id: UUID | None = None
    task: str
    mcp_refs: list[dict[str, Any]] | None = None
    mode: ConversationMode = ConversationMode.DEFAULT
    created_at: datetime = Field(default_factory=utc_now)
    run: RunProjection = Field(default_factory=RunProjection)
