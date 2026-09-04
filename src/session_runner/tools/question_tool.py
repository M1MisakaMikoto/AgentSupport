"""Human-confirmation tool for skill-generation conversations.

``ask_user`` pauses the run at a human gate (same durable pause used by tool
approvals) and feeds the user's answer back to the agent as the tool result.
The tool itself never executes a side effect; ``TraeToolGatewayBridge``
intercepts calls before dispatch.
"""

from __future__ import annotations

try:
    from trae_agent.tools.base import (
        Tool,
        ToolCallArguments,
        ToolError,
        ToolExecResult,
        ToolParameter,
    )
except ImportError:
    from session_runner.adapters.trae import _ensure_vendored_trae_path

    _ensure_vendored_trae_path()
    from trae_agent.tools.base import (
        Tool,
        ToolCallArguments,
        ToolError,
        ToolExecResult,
        ToolParameter,
    )


class AskUserTool(Tool):
    """Ask the human operator to confirm the intended skill scope before writing."""

    def __init__(self, model_provider: str | None = None) -> None:
        super().__init__(model_provider)

    def get_name(self) -> str:
        return "ask_user"

    def get_description(self) -> str:
        return (
            "向用户确认意图后继续。参数 `question` 用中文给出你想让用户确认的内容"
            "（例如打算总结进 SKILL 的场景、边界与失败教训）。调用后运行会暂停，"
            "直到用户回复：回复是「同意，按此范围生成」表示按你的方案继续；"
            "回复为其他文本时，请把用户文本当作纠正意见并按其调整后再继续。"
            "该工具必须单独调用，不能与其他工具混在同一批。"
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="question",
                type="string",
                description=(
                    "请用户确认的中文问题：列出你准备总结的场景、边界与失败教训。"
                ),
                required=True,
            )
        ]

    async def execute(self, arguments: ToolCallArguments) -> ToolExecResult:
        raise ToolError("ask_user is gated by the runner; it never executes a tool handler")


def register_question_tool() -> None:
    """Register the ask_user tool into the Trae tools registry (idempotent)."""
    from trae_agent.tools import tools_registry

    tools_registry.setdefault("ask_user", AskUserTool)
