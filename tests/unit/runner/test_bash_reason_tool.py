"""``bash`` must expose the mandatory ``reason`` parameter to the model."""

from __future__ import annotations

from session_runner.adapters.trae import _ensure_vendored_trae_path

_ensure_vendored_trae_path()

from trae_agent.tools import tools_registry
from trae_agent.tools.bash_tool import BashTool

from session_runner.tools.bash_reason_tool import (
    SupportBashTool,
    register_bash_tool,
)


def test_registration_replaces_vendored_bash_tool():
    register_bash_tool()

    assert tools_registry["bash"] is SupportBashTool
    assert issubclass(tools_registry["bash"], BashTool)


def test_reason_is_required_and_vendored_parameters_survive():
    tool = SupportBashTool(model_provider="anthropic")
    parameters = {parameter.name: parameter for parameter in tool.get_parameters()}

    assert parameters["command"].required is True
    assert parameters["reason"].required is True
    assert parameters["reason"].type == "string"


def test_description_asks_for_a_reason_and_grep_first():
    description = SupportBashTool(model_provider="anthropic").get_description()

    assert "reason" in description
    assert "grep" in description
