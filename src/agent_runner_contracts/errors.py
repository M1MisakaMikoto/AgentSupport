from typing import Any

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool = False
    correlation_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
