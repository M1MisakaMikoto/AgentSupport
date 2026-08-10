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


def test_anthropic_client_echoes_thinking_blocks_with_tool_results() -> None:
    types = trae_types()
    config = model_config(types, "anthropic")
    client = types.AnthropicClient(config)
    captured = CapturingMessages()
    client.client = SimpleNamespace(messages=captured)

    thinking = SimpleNamespace(
        type="thinking",
        thinking="I should inspect the workspace first.",
        signature="sig-0001",
    )
    tool_use = SimpleNamespace(
        type="tool_use",
        id="call-1",
        name="bash",
        input={"command": "ls"},
    )

    calls = {"count": 0}

    def fake_create(**kwargs):
        captured.request = kwargs
        captured.request_messages = list(kwargs["messages"])
        calls["count"] += 1
        if calls["count"] == 1:
            return SimpleNamespace(
                content=[thinking, tool_use],
                usage=None,
                model=config.model,
                stop_reason="tool_use",
            )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="completed")],
            usage=None,
            model=config.model,
            stop_reason="end_turn",
        )

    captured.create = fake_create

    first = client.chat(
        [types.LLMMessage(role="user", content="Inspect the workspace")],
        config,
        tools=[types.BashTool("anthropic")],
    )
    assert first.tool_calls is not None
    assert first.tool_calls[0].call_id == "call-1"

    client.chat(
        [
            types.LLMMessage(
                role="user",
                tool_result=SimpleNamespace(
                    call_id="call-1",
                    name="bash",
                    success=True,
                    result="ok",
                    error=None,
                ),
            )
        ],
        config,
        tools=[types.BashTool("anthropic")],
    )

    assert captured.request_messages is not None
    assistant = captured.request_messages[-2]
    assert assistant["role"] == "assistant"
    blocks = assistant["content"]
    assert [block.type for block in blocks] == ["thinking", "tool_use"]
    assert blocks[0].thinking == "I should inspect the workspace first."
    assert blocks[0].signature == "sig-0001"
    assert blocks[1].id == "call-1"
    assert captured.request_messages[-1]["role"] == "user"
    assert captured.request_messages[-1]["content"][0]["type"] == "tool_result"


def test_anthropic_client_batches_parallel_tool_results_in_one_user_message() -> None:
    types = trae_types()
    config = model_config(types, "anthropic")
    client = types.AnthropicClient(config)
    captured = CapturingMessages()
    client.client = SimpleNamespace(messages=captured)

    first_tool_use = SimpleNamespace(
        type="tool_use",
        id="call-1",
        name="bash",
        input={"command": "ls"},
    )
    second_tool_use = SimpleNamespace(
        type="tool_use",
        id="call-2",
        name="str_replace_based_edit_tool",
        input={"command": "view", "path": "/workspace"},
    )
    calls = {"count": 0}

    def fake_create(**kwargs):
        captured.request = kwargs
        captured.request_messages = list(kwargs["messages"])
        calls["count"] += 1
        if calls["count"] == 1:
            return SimpleNamespace(
                content=[first_tool_use, second_tool_use],
                usage=None,
                model=config.model,
                stop_reason="tool_use",
            )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="completed")],
            usage=None,
            model=config.model,
            stop_reason="end_turn",
        )

    captured.create = fake_create

    first = client.chat(
        [types.LLMMessage(role="user", content="Inspect the workspace")],
        config,
        tools=[types.BashTool("anthropic"), types.TextEditorTool("anthropic")],
    )
    assert [call.call_id for call in first.tool_calls] == ["call-1", "call-2"]

    client.chat(
        [
            types.LLMMessage(
                role="user",
                tool_result=SimpleNamespace(
                    call_id="call-1",
                    name="bash",
                    success=True,
                    result="ok",
                    error=None,
                ),
            ),
            types.LLMMessage(
                role="user",
                tool_result=SimpleNamespace(
                    call_id="call-2",
                    name="str_replace_based_edit_tool",
                    success=True,
                    result="ok",
                    error=None,
                ),
            ),
        ],
        config,
        tools=[types.BashTool("anthropic"), types.TextEditorTool("anthropic")],
    )

    assert captured.request_messages is not None
    assistant = captured.request_messages[-2]
    tool_results = captured.request_messages[-1]
    assert assistant["role"] == "assistant"
    assert [block.id for block in assistant["content"]] == ["call-1", "call-2"]
    assert tool_results["role"] == "user"
    assert [block["type"] for block in tool_results["content"]] == [
        "tool_result",
        "tool_result",
    ]
    assert [block["tool_use_id"] for block in tool_results["content"]] == ["call-1", "call-2"]
