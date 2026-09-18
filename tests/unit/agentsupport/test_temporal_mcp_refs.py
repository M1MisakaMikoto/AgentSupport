"""Temporal 路径解析 mcp_refs 时必须带上 **stdio** 字段。

回归：控制面有两条解析实现（服务侧 `skill_mcp_ops._resolve_mcp_refs` 与 worker 侧
`activities._resolve_mcp_refs`）。只改前者的话，runner 收到的是"没有 command 的 stdio
配置"，vendored 客户端抛 ValueError 并被 `discover_mcp_tools` 静默吞掉 ——
表现是"模型看不到任何 MCP 工具"，且没有任何日志。
"""
from types import SimpleNamespace

import pytest

from agentsupport.domain import McpServer
from agentsupport.execution.temporal.activities import _resolve_mcp_refs


def _ctx(server: McpServer) -> SimpleNamespace:
    return SimpleNamespace(repository=SimpleNamespace(get_mcp_server=lambda sid: server))


@pytest.mark.parametrize("transport,specific", [
    ("http", {"http_url": "http://mcp:8000/mcp"}),
    ("sse", {"sse_url": "http://mcp:8000/sse"}),
    ("stdio", {"command": "python", "args": ["/app/main.py"],
               "env": {"TOKEN": "$X"}, "cwd": "/app"}),
])
def test_refs_carry_transport_specific_fields(transport, specific) -> None:
    server = McpServer(server_id="document-assistant", name="docs",
                       transport=transport, headers={"A": "b"}, **specific)
    resolved = _resolve_mcp_refs([{"server_id": "document-assistant"}], _ctx(server))
    assert len(resolved) == 1
    ref = resolved[0]
    assert ref["transport"] == transport
    assert ref["headers"] == {"A": "b"}
    for key, value in specific.items():
        assert ref[key] == value, key


def test_unknown_server_is_skipped_not_guessed() -> None:
    ctx = SimpleNamespace(repository=SimpleNamespace(get_mcp_server=lambda sid: None))
    assert _resolve_mcp_refs([{"server_id": "nope"}], ctx) == []
