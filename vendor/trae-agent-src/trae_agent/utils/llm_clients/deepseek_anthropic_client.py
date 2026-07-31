# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""DeepSeek's Anthropic-compatible API client."""

from typing import override

import anthropic

from trae_agent.tools.base import Tool
from trae_agent.utils.llm_clients.anthropic_client import AnthropicClient


class DeepSeekAnthropicClient(AnthropicClient):
    """Anthropic Messages client using DeepSeek's supported custom-tool subset."""

    provider_name = "deepseek_anthropic"
    retry_provider_name = "DeepSeek Anthropic"

    @override
    def _build_tool_schema(self, tool: Tool) -> anthropic.types.ToolParam:
        return anthropic.types.ToolParam(
            name=tool.name,
            description=tool.description,
            input_schema=tool.get_input_schema(),
        )
