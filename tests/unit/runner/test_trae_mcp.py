"""Trae adapter dynamic MCP server injection tests."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from session_runner.adapters.trae import TraeExecutionAdapter
from session_runner.trae_runtime import TraeRuntimeSettings


class _FakeToolCaller:
    async def sequential_tool_call(self, calls):
        return []

    async def parallel_tool_call(self, calls):
        return []

    async def close_tools(self):
        return None


class _FakeMcpAgent:
    def __init__(self):
        self.mcp_servers_config = {}
        self.allow_mcp_servers = []
        self.initialised_mcp = False
        self.tools = [SimpleNamespace(name="bash")]
        self._tool_caller = _FakeToolCaller()

    async def initialise_mcp(self):
        self.initialised_mcp = True
        self.tools.append(SimpleNamespace(name="mcp.gitlab.list_projects"))


@pytest.mark.asyncio
async def test_trae_adapter_injects_mcp_config_and_authorizes_tools(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = tmp_path / "trae_config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    settings = TraeRuntimeSettings(
        config_path=config_path,
        provider="test",
        model="test-model",
        model_base_url=None,
        api_key="key",
        max_steps=3,
        workspace_roots=(workspace.resolve(),),
    )
    request = SimpleNamespace(
        context_bundle={},
        workspace_ref=str(workspace),
        tool_policy={},
        run_id=uuid4(),
    )

    def factory(runtime_settings, request, trajectory):
        agent = SimpleNamespace(agent=_FakeMcpAgent())
        return agent

    adapter = TraeExecutionAdapter(
        request,
        emit=lambda event_type, payload=None: None,
        on_waiting=lambda interaction, batch, next_step: None,
        settings=settings,
        agent_factory=factory,
        mcp_servers_config={
            "gitlab": {
                "http_url": "http://mcp-gitlab:8000/mcp",
                "headers": {"Authorization": "$GITLAB_TOKEN"},
            }
        },
    )
    await adapter._initialize_agent()

    agent = adapter.agent.agent
    assert agent.mcp_servers_config["gitlab"]["http_url"] == "http://mcp-gitlab:8000/mcp"
    assert agent.initialised_mcp is True
    assert agent.allow_mcp_servers == []
    assert "mcp.gitlab.list_projects" in adapter.bridge.policy.allowed_tools
    assert any(
        tool.name == "mcp.gitlab.list_projects"
        for tool in adapter.agent.agent.tools
    )
