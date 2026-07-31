from types import SimpleNamespace

import pytest

from session_runner.adapters.trae import _ensure_vendored_trae_path


class CapturingMessages:
    def __init__(self) -> None:
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(
            content=[],
            usage=None,
            model=kwargs["model"],
            stop_reason="end_turn",
        )


def trae_types():
    pytest.importorskip("anthropic")
    _ensure_vendored_trae_path()

    from trae_agent.tools.bash_tool import BashTool
    from trae_agent.tools.edit_tool import TextEditorTool
    from trae_agent.utils.config import ModelConfig, ModelProvider
    from trae_agent.utils.llm_clients.anthropic_client import AnthropicClient
    from trae_agent.utils.llm_clients.deepseek_anthropic_client import (
        DeepSeekAnthropicClient,
    )
    from trae_agent.utils.llm_clients.llm_basics import LLMMessage
    from trae_agent.utils.llm_clients.llm_client import LLMClient

    return SimpleNamespace(
        AnthropicClient=AnthropicClient,
        BashTool=BashTool,
        DeepSeekAnthropicClient=DeepSeekAnthropicClient,
        LLMClient=LLMClient,
        LLMMessage=LLMMessage,
        ModelConfig=ModelConfig,
        ModelProvider=ModelProvider,
        TextEditorTool=TextEditorTool,
    )


def model_config(types, provider: str):
    return types.ModelConfig(
        model="claude-compatible-model",
        model_provider=types.ModelProvider(api_key="test-key", provider=provider),
        temperature=0,
        top_p=1,
        top_k=0,
        parallel_tool_calls=False,
        max_retries=0,
        max_tokens=128,
    )


def capture_tool_schemas(types, client_type, provider: str):
    config = model_config(types, provider)
    client = client_type(config)
    messages = CapturingMessages()
    client.client = SimpleNamespace(messages=messages)

    client.chat(
        [types.LLMMessage(role="user", content="Inspect the workspace")],
        config,
        tools=[types.BashTool(provider), types.TextEditorTool(provider)],
    )

    assert messages.request is not None
    return messages.request["tools"]


def test_official_anthropic_uses_native_tool_schemas() -> None:
    types = trae_types()

    schemas = capture_tool_schemas(types, types.AnthropicClient, "anthropic")

    assert schemas == [
        {"name": "bash", "type": "bash_20250124"},
        {
            "name": "str_replace_based_edit_tool",
            "type": "text_editor_20250429",
        },
    ]


def test_deepseek_anthropic_uses_custom_tool_schemas() -> None:
    types = trae_types()

    schemas = capture_tool_schemas(
        types,
        types.DeepSeekAnthropicClient,
        "deepseek_anthropic",
    )

    assert [schema["name"] for schema in schemas] == [
        "bash",
        "str_replace_based_edit_tool",
    ]
    assert all("type" not in schema for schema in schemas)
    assert all(schema["description"] for schema in schemas)
    assert schemas[0]["input_schema"]["properties"]["command"]["type"] == "string"
    assert schemas[1]["input_schema"]["properties"]["path"]["type"] == "string"
    assert schemas[1]["input_schema"]["required"] == ["command", "path"]


def test_deepseek_anthropic_provider_selects_compatible_client() -> None:
    types = trae_types()

    client = types.LLMClient(model_config(types, "deepseek_anthropic"))

    assert isinstance(client.client, types.DeepSeekAnthropicClient)
