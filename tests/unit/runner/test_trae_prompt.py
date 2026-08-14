from types import SimpleNamespace
from uuid import uuid4

import pytest

from session_runner.adapters.trae import (
    TraeExecutionAdapter,
    TraeRuntimeSettings,
    _resolve_system_prompt,
    _skill_prompt_section,
)


def make_settings(tmp_path, *, prompt_file=None):
    config = tmp_path / "trae_config.yaml"
    config.write_text("agents: {}\n", encoding="utf-8")
    return TraeRuntimeSettings(
        config_path=config,
        provider="test",
        model="test-model",
        model_base_url=None,
        api_key="test-key",
        max_steps=3,
        workspace_roots=(tmp_path.resolve(),),
        system_prompt_file=prompt_file,
    )


class _Request:
    def __init__(self, context_bundle=None):
        self.context_bundle = context_bundle or {}


def test_prompt_file_wins_over_context_bundle(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("custom file prompt", encoding="utf-8")
    settings = make_settings(tmp_path, prompt_file=prompt_file)
    request = _Request({"system_prompt": "bundle prompt"})
    assert _resolve_system_prompt(settings, request) == "custom file prompt"


def test_context_bundle_prompt_used_when_no_file(tmp_path):
    settings = make_settings(tmp_path)
    request = _Request({"system_prompt": "bundle prompt"})
    assert _resolve_system_prompt(settings, request) == "bundle prompt"


def test_falls_back_to_builtin_when_no_override(tmp_path):
    settings = make_settings(tmp_path)
    assert _resolve_system_prompt(settings, _Request({})) is None


def test_skill_prompt_section_builds_from_context_bundle():
    section = _skill_prompt_section(
        {
            "skills": [
                {
                    "skill_id": "review",
                    "mount_path": "/opt/agent-skills/review",
                    "content": "# Review\n输出审查报告。",
                }
            ]
        }
    )
    assert section is not None
    assert "## Skill: review" in section
    assert "/opt/agent-skills/review" in section
    assert "输出审查报告" in section


def test_skill_prompt_section_falls_back_to_manifest():
    section = _skill_prompt_section(
        {"skill_manifest": [{"skill_id": "review", "mount_path": "/opt/agent-skills/review"}]}
    )
    assert section is not None
    assert "内容未随请求携带" in section


def test_skill_prompt_section_none_without_skills():
    assert _skill_prompt_section({"task": "hi"}) is None


def test_blank_context_bundle_prompt_is_ignored(tmp_path):
    settings = make_settings(tmp_path)
    request = _Request({"system_prompt": "   "})
    assert _resolve_system_prompt(settings, request) is None


def test_missing_prompt_file_raises(tmp_path):
    settings = make_settings(tmp_path, prompt_file=tmp_path / "missing.txt")
    with pytest.raises(ValueError, match="does not exist"):
        _resolve_system_prompt(settings, _Request({}))


def test_from_environment_reads_prompt_file(monkeypatch, tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("env prompt", encoding="utf-8")
    monkeypatch.setenv("SESSION_RUNNER_TRAE_PROMPT_FILE", str(prompt_file))
    settings = TraeRuntimeSettings.from_environment()
    assert settings.system_prompt_file == prompt_file


@pytest.mark.asyncio
async def test_run_passes_neutral_extra_args_without_issue(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}

    class FakeAgent:
        def __init__(self):
            self.agent = SimpleNamespace(tools=[], _tool_caller=None)

        async def run(self, task, extra_args):
            captured["task"] = task
            captured["extra_args"] = extra_args
            return SimpleNamespace(success=True, final_result="ok", steps=[1])

    def factory(settings, request, trajectory):
        return FakeAgent()

    request = SimpleNamespace(
        run_id=uuid4(),
        workspace_ref=str(workspace),
        context_bundle={"task": "hello"},
        tool_policy={},
    )
    adapter = TraeExecutionAdapter(
        request,
        lambda event_type, payload=None: None,
        lambda interaction, batch, next_step: None,
        settings=make_settings(tmp_path),
        agent_factory=factory,
    )

    result = await adapter.run()

    assert captured["task"] == "hello"
    assert captured["extra_args"] == {"project_path": str(workspace)}
    assert "issue" not in captured["extra_args"]
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_initialize_agent_injects_skills_into_system_prompt(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class FakeTraeAgent:
        def __init__(self):
            self._system_prompt = None
            self.tools = []
            self._tool_caller = None

        def get_system_prompt(self):
            return self._system_prompt or "base prompt"

    class FakeAgent:
        def __init__(self):
            self.agent = FakeTraeAgent()

        async def run(self, task, extra_args):
            return SimpleNamespace(success=True, final_result="ok", steps=[1])

    def factory(settings, request, trajectory):
        return FakeAgent()

    request = SimpleNamespace(
        run_id=uuid4(),
        workspace_ref=str(workspace),
        context_bundle={
            "task": "review the code",
            "skills": [
                {
                    "skill_id": "review",
                    "mount_path": "/opt/agent-skills/review",
                    "content": "# Review\n输出审查报告。",
                }
            ],
        },
        tool_policy={},
    )
    adapter = TraeExecutionAdapter(
        request,
        lambda event_type, payload=None: None,
        lambda interaction, batch, next_step: None,
        settings=make_settings(tmp_path),
        agent_factory=factory,
    )

    await adapter._initialize_agent()

    prompt = adapter.agent.agent._system_prompt
    assert prompt.startswith("base prompt")
    assert "## Skill: review" in prompt
    assert "输出审查报告" in prompt
