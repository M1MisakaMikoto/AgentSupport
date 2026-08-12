"""MCP server registration domain model."""

from datetime import datetime

from pydantic import BaseModel, Field

from .execution import utc_now

SUPPORTED_TRANSPORTS = ("http", "sse")


class McpServer(BaseModel):
    server_id: str
    name: str
    transport: str
    http_url: str | None = None
    sse_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def endpoint(self) -> str | None:
        if self.transport == "http":
            return self.http_url
        if self.transport == "sse":
            return self.sse_url
        return None
