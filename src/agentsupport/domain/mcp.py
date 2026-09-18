"""MCP server registration domain model.

三种 transport：
  - ``http`` / ``sse``：服务是独立进程/容器，用 URL 连；
  - ``stdio``：服务由 runner **当作子进程拉起**（`command/args/env/cwd`）。

为什么必须有 stdio：trae 模式的 runner 用的 vendored MCP 客户端**只实现了 stdio**
（http/url 直接 ``NotImplementedError``，且异常被 ``discover_mcp_tools`` 静默吞掉，
表现为"模型看不到任何 MCP 工具"）。要保住"一租户一策略、按会话下发 MCP"，
注册表就得能表达 stdio。
"""

from datetime import datetime

from pydantic import BaseModel, Field

from .execution import utc_now

SUPPORTED_TRANSPORTS = ("http", "sse", "stdio")


class McpServer(BaseModel):
    server_id: str
    name: str
    transport: str
    http_url: str | None = None
    sse_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    #: stdio transport：runner 用这些字段把 MCP 服务当子进程拉起来
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    description: str = ""
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def endpoint(self) -> str | None:
        if self.transport == "http":
            return self.http_url
        if self.transport == "sse":
            return self.sse_url
        if self.transport == "stdio":
            return self.command
        return None
