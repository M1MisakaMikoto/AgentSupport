from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SessionCreate(BaseModel):
    workspace_id: UUID


class ConversationCreate(BaseModel):
    task: str = Field(min_length=1)
    parent_conversation_id: UUID | None = None


class InteractionRequest(BaseModel):
    interaction_id: str
    value: Any
    expected_seq: int | None = None


class ApprovalRequest(BaseModel):
    approval_id: str
    decision: str
    expected_seq: int | None = None


class CancelRequest(BaseModel):
    expected_seq: int | None = None
