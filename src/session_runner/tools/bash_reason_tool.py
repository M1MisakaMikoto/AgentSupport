"""``bash`` with a mandatory one-line reason.

The vendored ``BashTool`` declares only ``command`` / ``restart``. The command
gate needs the model's stated intent as one of its three inputs, so the runner
swaps in this subclass: the schema gains a required ``reason`` parameter while
execution is inherited unchanged. No vendor file is edited.
"""

from __future__ import annotations

try:
    from trae_agent.tools.base import ToolParameter
    from trae_agent.tools.bash_tool import BashTool
except ImportError:  # pragma: no cover - depends on import order
    from session_runner.adapters.trae import _ensure_vendored_trae_path

    _ensure_vendored_trae_path()
    from trae_agent.tools.base import ToolParameter
    from trae_agent.tools.bash_tool import BashTool


REASON_PARAMETER = ToolParameter(
    name="reason",
    type="string",
    description=(
        "用一句话说明你为什么需要执行这条命令：要读取什么、为什么与当前任务相关。"
        "该理由会作为审批依据之一，缺少理由的调用会被拒绝。"
    ),
    required=True,
)

REASON_HINT = (
    "\n* Every call must carry a `reason` parameter: one sentence saying what it reads and why."
    "\n* Prefer `grep` to locate the part you need before reading a large file in full."
)


class SupportBashTool(BashTool):
    """Vendored ``bash`` plus a required ``reason`` argument."""

    def get_description(self) -> str:
        return f"{super().get_description()}{REASON_HINT}"

    def get_parameters(self) -> list[ToolParameter]:
        return [*super().get_parameters(), REASON_PARAMETER]


def register_bash_tool() -> None:
    """Replace the vendored ``bash`` entry with the reason-carrying subclass."""

    from trae_agent.tools import tools_registry

    tools_registry["bash"] = SupportBashTool
