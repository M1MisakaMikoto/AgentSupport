"""Unit tests for per-run MCP config resolution and gateway connections."""

from types import SimpleNamespace

import pytest

from session_runner import mcp_runtime


def test_build_mcp_server_configs_resolves_headers(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    configs = mcp_runtime.build_mcp_server_configs(
        [
            {
                "server_id": "gitlab",
                "transport": "http",
                "http_url": "http://mcp-gitlab:8000/mcp",
                "headers": {"Authorization": "$GITLAB_TOKEN", "X-Static": "v"},
            },
            {
                "server_id": "jira",
                "transport": "sse",
                "sse_url": "http://mcp-jira:8000/sse",
            },
        ]
    )
    assert configs["gitlab"]["http_url"] == "http://mcp-gitlab:8000/mcp"
    assert configs["gitlab"]["headers"] == {"Authorization": "secret", "X-Static": "v"}
    assert configs["jira"]["url"] == "http://mcp-jira:8000/sse"

    with pytest.raises(RuntimeError, match="not set"):
        mcp_runtime.build_mcp_server_configs(
            [{"server_id": "x", "transport": "http", "headers": {"A": "$MISSING_ENV"}}]
        )


def test_build_mcp_server_configs_supports_stdio(monkeypatch):
    """stdio：runner 把 MCP 服务拉成子进程（trae 模式唯一可用的 transport）。"""
    monkeypatch.setenv("DOC_HOST_TOKEN", "svc-token")
    configs = mcp_runtime.build_mcp_server_configs(
        [
            {
                "server_id": "document-assistant",
                "transport": "stdio",
                "command": "python",
                "args": ["/app/da-mcp-server/main.py", "--transport", "stdio"],
                "env": {"DOC_HOST_TOKEN": "$DOC_HOST_TOKEN", "LANG": "C.UTF-8"},
                "cwd": "/app/da-mcp-server",
            }
        ]
    )
    assert configs["document-assistant"] == {
        "command": "python",
        "args": ["/app/da-mcp-server/main.py", "--transport", "stdio"],
        "env": {"DOC_HOST_TOKEN": "svc-token", "LANG": "C.UTF-8"},
        "cwd": "/app/da-mcp-server",
    }


def test_stdio_env_placeholder_must_be_set(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="not set"):
        mcp_runtime.build_mcp_server_configs(
            [{"server_id": "x", "transport": "stdio", "command": "python",
              "env": {"TOKEN": "$MISSING_TOKEN"}}]
        )


class _FakeClientCtx:
    def __init__(self):
        self.closed = False

    async def __aenter__(self):
        return "read", "write", lambda: "sid"

    async def __aexit__(self, *args):
        self.closed = True
        return False


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return SimpleNamespace(
            tools=[SimpleNamespace(name="list_projects", description="", inputSchema={})]
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(structuredContent={"result": f"hi {arguments['name']}"})


@pytest.mark.asyncio
async def test_build_mcp_provider_connects_exposes_and_closes(monkeypatch):
    client_ctx = _FakeClientCtx()
    session = _FakeSession()
    monkeypatch.setattr(
        mcp_runtime, "streamablehttp_client", lambda url, headers=None: client_ctx
    )
    monkeypatch.setattr(mcp_runtime, "ClientSession", lambda read, write: session)

    provider = await mcp_runtime.build_mcp_provider(
        [
            {
                "server_id": "gitlab",
                "transport": "http",
                "http_url": "http://mcp-gitlab:8000/mcp",
            }
        ]
    )
    assert provider is not None
    assert provider.allowed_servers == {"gitlab"}
    descriptors = provider.descriptors(["gitlab"])
    assert any(item.name == "mcp.gitlab.list_projects" for item in descriptors)

    handler = provider.handlers(["gitlab"])["mcp.gitlab.list_projects"]
    result = await handler({"name": "agent"})
    assert result == {"result": "hi agent"}
    assert session.calls == [("list_projects", {"name": "agent"})]

    await provider.close()
    assert session.closed is True
    assert client_ctx.closed is True
