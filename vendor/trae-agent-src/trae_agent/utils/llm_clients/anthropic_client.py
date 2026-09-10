# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Anthropic API client wrapper with tool integration."""

import json
import os
from typing import Any, Callable, override

import anthropic
import httpx
from anthropic.types.tool_union_param import TextEditor20250429

from trae_agent.tools.base import Tool, ToolCall, ToolResult
from trae_agent.utils.config import ModelConfig
from trae_agent.utils.llm_clients.base_client import BaseLLMClient
from trae_agent.utils.llm_clients.llm_basics import LLMMessage, LLMResponse, LLMUsage
from trae_agent.utils.llm_clients.retry_utils import retry_with


class AnthropicClient(BaseLLMClient):
    """Anthropic client wrapper with tool schema generation."""

    provider_name = "anthropic"
    retry_provider_name = "Anthropic"

    def __init__(self, model_config: ModelConfig):
        super().__init__(model_config)

        client_options: dict[str, Any] = {
            "api_key": self.api_key,
            "base_url": self.base_url,
            # Explicit timeouts: the SDK default is read=600s, which lets one
            # stalled stream freeze the whole runner for ten minutes.
            "timeout": httpx.Timeout(
                connect=float(os.getenv("TRAE_LLM_CONNECT_TIMEOUT_SECONDS", "5")),
                read=float(os.getenv("TRAE_LLM_READ_TIMEOUT_SECONDS", "120")),
                write=float(os.getenv("TRAE_LLM_WRITE_TIMEOUT_SECONDS", "30")),
                pool=float(os.getenv("TRAE_LLM_POOL_TIMEOUT_SECONDS", "15")),
            ),
            # The trae layer retries on top of the SDK, so keep SDK retries at
            # one to avoid multiplying a stalled stream's wait window.
            "max_retries": int(os.getenv("TRAE_LLM_SDK_MAX_RETRIES", "1")),
        }
        host_override = os.getenv("TRAE_MODEL_HOST", "").strip()
        if host_override:
            # Corporate TLS inspection often keys off the SNI hostname. When the
            # provider must be reached through a CDN CNAME, the SNI uses the URL
            # host while the origin still expects the original Host header.
            client_options["default_headers"] = {"Host": host_override}
        self.client: anthropic.Anthropic = anthropic.Anthropic(**client_options)
        self.message_history: list[anthropic.types.MessageParam] = []
        self.system_message: str | anthropic.NotGiven = anthropic.NOT_GIVEN
        self.on_text_delta: Callable[[str], None] | None = None

    @override
    def set_chat_history(self, messages: list[LLMMessage]) -> None:
        """Set the chat history."""
        self.message_history = self.parse_messages(messages)

    def _build_tool_schema(self, tool: Tool) -> anthropic.types.ToolUnionParam:
        if tool.name == "str_replace_based_edit_tool":
            return TextEditor20250429(
                name="str_replace_based_edit_tool",
                type="text_editor_20250429",
            )
        if tool.name == "bash":
            return anthropic.types.ToolBash20250124Param(
                name="bash",
                type="bash_20250124",
            )
        return anthropic.types.ToolParam(
            name=tool.name,
            description=tool.description,
            input_schema=tool.get_input_schema(),
        )

    def _create_anthropic_response(
        self,
        model_config: ModelConfig,
        tool_schemas: list[anthropic.types.ToolUnionParam] | anthropic.NotGiven,
    ) -> anthropic.types.Message:
        """Create a response using Anthropic API. This method will be decorated with retry logic."""
        return self.client.messages.create(
            model=model_config.model,
            messages=self.message_history,
            max_tokens=model_config.max_tokens,
            system=self.system_message,
            tools=tool_schemas,
            temperature=model_config.temperature,
            top_p=model_config.top_p,
            top_k=model_config.top_k,
        )

    def _create_anthropic_response_stream(
        self,
        model_config: ModelConfig,
        tool_schemas: list[anthropic.types.ToolUnionParam] | anthropic.NotGiven,
    ) -> anthropic.types.Message:
        """Create a streamed Anthropic response, forwarding text deltas live.

        The final message (including tool_use blocks) is reconstructed by the
        SDK so downstream parsing stays identical to the batch path.
        """
        with self.client.messages.stream(
            model=model_config.model,
            messages=self.message_history,
            max_tokens=model_config.max_tokens,
            system=self.system_message,
            tools=tool_schemas,
            temperature=model_config.temperature,
            top_p=model_config.top_p,
            top_k=model_config.top_k,
        ) as stream:
            for event in stream:
                if event.type != "content_block_delta":
                    continue
                delta_type = getattr(event.delta, "type", None)
                delta_text = getattr(event.delta, "text", None)
                if delta_type == "text_delta" and delta_text:
                    if self.on_text_delta is not None:
                        self.on_text_delta(delta_text)
            return stream.get_final_message()

    @override
    def chat(
        self,
        messages: list[LLMMessage],
        model_config: ModelConfig,
        tools: list[Tool] | None = None,
        reuse_history: bool = True,
    ) -> LLMResponse:
        """Send chat messages to Anthropic with optional tool support."""
        # Convert messages to Anthropic format
        anthropic_messages: list[anthropic.types.MessageParam] = self.parse_messages(messages)

        self.message_history = (
            self.message_history + anthropic_messages if reuse_history else anthropic_messages
        )

        # Add tools if provided
        tool_schemas: list[anthropic.types.ToolUnionParam] | anthropic.NotGiven = (
            anthropic.NOT_GIVEN
        )
        if tools:
            tool_schemas = [self._build_tool_schema(tool) for tool in tools]

        # Apply retry decorator to the API call. When a streaming consumer is
        # attached (on_text_delta), use the streaming variant so text deltas are
        # forwarded in real time; otherwise keep the original batch call.
        # Always stream. Measured on the deployed gateway: non-streaming
        # responses stall (~50% of medium/long outputs return 200 with an empty
        # body), while the SSE path completed 13/13 including 8k-token outputs.
        # Deltas are forwarded to the caller only when a sink is attached.
        response_factory = self._create_anthropic_response_stream
        retry_decorator = retry_with(
            func=response_factory,
            provider_name=self.retry_provider_name,
            max_retries=model_config.max_retries,
        )
        response = retry_decorator(model_config, tool_schemas)

        # Handle tool calls in response. The whole assistant turn is recorded as a single
        # message containing every content block in order. In extended-thinking mode the
        # `thinking` blocks (with their signatures) must be echoed back alongside the
        # `tool_use` blocks, otherwise Anthropic-compatible providers reject the follow-up
        # message that carries the tool results.
        content = ""
        tool_calls: list[ToolCall] = []
        assistant_blocks = []

        for content_block in response.content:
            if content_block.type == "text":
                content += content_block.text
                assistant_blocks.append(content_block)
            elif content_block.type == "thinking":
                assistant_blocks.append(content_block)
            elif content_block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        call_id=content_block.id,
                        name=content_block.name,
                        arguments=content_block.input,  # pyright: ignore[reportArgumentType]
                    )
                )
                assistant_blocks.append(content_block)

        if assistant_blocks:
            self.message_history.append(
                anthropic.types.MessageParam(role="assistant", content=assistant_blocks)
            )

        usage = None
        if response.usage:
            usage = LLMUsage(
                input_tokens=response.usage.input_tokens or 0,
                output_tokens=response.usage.output_tokens or 0,
                cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
                cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            )

        llm_response = LLMResponse(
            content=content,
            usage=usage,
            model=response.model,
            finish_reason=response.stop_reason,
            tool_calls=tool_calls if len(tool_calls) > 0 else None,
        )

        # Record trajectory if recorder is available
        if self.trajectory_recorder:
            self.trajectory_recorder.record_llm_interaction(
                messages=messages,
                response=llm_response,
                provider=self.provider_name,
                model=model_config.model,
                tools=tools,
            )

        return llm_response

    def parse_messages(self, messages: list[LLMMessage]) -> list[anthropic.types.MessageParam]:
        """Parse the messages to Anthropic format."""
        anthropic_messages: list[anthropic.types.MessageParam] = []
        pending_tool_results: list[anthropic.types.ToolResultBlockParam] = []

        def flush_tool_results() -> None:
            if not pending_tool_results:
                return
            anthropic_messages.append(
                anthropic.types.MessageParam(role="user", content=list(pending_tool_results))
            )
            pending_tool_results.clear()

        for msg in messages:
            if msg.role == "system":
                self.system_message = msg.content if msg.content else anthropic.NOT_GIVEN
            elif msg.tool_result:
                # Anthropic-compatible APIs require every tool_result for a tool_use
                # batch to be delivered together in the single next user message.
                pending_tool_results.append(self.parse_tool_call_result(msg.tool_result))
            elif msg.tool_call:
                flush_tool_results()
                anthropic_messages.append(
                    anthropic.types.MessageParam(
                        role="assistant", content=[self.parse_tool_call(msg.tool_call)]
                    )
                )
            else:
                flush_tool_results()
                if msg.role == "user":
                    role = "user"
                elif msg.role == "assistant":
                    role = "assistant"
                else:
                    raise ValueError(f"Invalid message role: {msg.role}")

                if not msg.content:
                    raise ValueError("Message content is required")

                anthropic_messages.append(
                    anthropic.types.MessageParam(role=role, content=msg.content)
                )
        flush_tool_results()
        return anthropic_messages

    def parse_tool_call(self, tool_call: ToolCall) -> anthropic.types.ToolUseBlockParam:
        """Parse the tool call from the LLM response."""
        return anthropic.types.ToolUseBlockParam(
            type="tool_use",
            id=tool_call.call_id,
            name=tool_call.name,
            input=json.dumps(tool_call.arguments),
        )

    def parse_tool_call_result(
        self, tool_call_result: ToolResult
    ) -> anthropic.types.ToolResultBlockParam:
        """Parse the tool call result from the LLM response."""
        result: str = ""
        if tool_call_result.result:
            result = result + tool_call_result.result + "\n"
        if tool_call_result.error:
            result += "Tool call failed with error:\n"
            result += tool_call_result.error
        result = result.strip()

        # Provide a default error message if the tool failed but didn't provide details
        if not tool_call_result.success and not result:
            result = "Tool execution failed without providing error details."

        return anthropic.types.ToolResultBlockParam(
            tool_use_id=tool_call_result.call_id,
            type="tool_result",
            content=result,
            is_error=not tool_call_result.success,
        )
