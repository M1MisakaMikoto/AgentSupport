"""Guard the vendored Trae prompt's social-message completion contract.

The agent loop only completes a turn when the model calls ``task_done``; a
plain-text reply is treated as incomplete and pushes the model into tool
exploration. The triage prompt must therefore close social exchanges with a
single ``task_done`` and forbid every other tool.
"""

from pathlib import Path

PROMPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "vendor"
    / "trae-agent-src"
    / "trae_agent"
    / "prompt"
    / "agent_prompt.py"
)


def test_social_triage_requires_task_done_and_forbids_other_tools():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    social_section = prompt.split("1. Social / small talk")[1].split("2. Knowledge")[0]
    assert "call `task_done`" in social_section
    assert "task_done` is the ONLY allowed tool call" in social_section
    assert "bash" in social_section


def test_completion_section_does_not_forbid_task_done_for_social():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    completion = prompt.split("# Completion")[1]
    assert "never call `task_done`" not in completion
    assert "call `task_done` together with your reply" in completion
