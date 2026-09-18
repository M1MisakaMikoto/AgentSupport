"""Tenant-declared CLI safe prefixes and the run-scoped scratch directory."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from session_runner.adapters.trae import TraeExecutionAdapter
from session_runner.security import (
    AllowAllApprover,
    BashCallGate,
    CommandTier,
    classify_command,
)
from session_runner.trae_runtime import TraeRuntimeSettings


def _classify(command: str, tmp_path: Path, *, declared: tuple[str, ...] = ()):
    return classify_command(
        command,
        allowed_prefixes=[str(tmp_path)],
        cwd=tmp_path,
        declared_safe_prefixes=declared,
    )


def test_declared_prefix_is_safe(tmp_path):
    verdict = _classify("mytool list --all", tmp_path, declared=("mytool list",))
    assert verdict.tier is CommandTier.SAFE
    assert "declared safe prefix" in verdict.reason

    # A sub-command that was not declared stays risky.
    assert (
        _classify("mytool delete --all", tmp_path, declared=("mytool list",)).tier
        is CommandTier.RISKY
    )
    # A prefix must match token-wise from the start.
    assert (
        _classify("mytool listing", tmp_path, declared=("mytool list",)).tier
        is CommandTier.RISKY
    )


def test_deployment_lists_still_win(tmp_path):
    # Denied executables never auto-approve, even when a tenant declares them.
    denied = _classify("bash -c 'rm -rf /'", tmp_path, declared=("bash -c",))
    assert denied.tier is CommandTier.BLOCKED

    # Composition characters are never auto-approved.
    composed = _classify("mytool list | head", tmp_path, declared=("mytool list",))
    assert composed.tier is CommandTier.RISKY


def test_network_clis_are_risky_not_denied(tmp_path):
    for command in ("curl https://example.com/pkg.tgz", "wget https://example.com/pkg.tgz"):
        verdict = _classify(command, tmp_path)
        assert verdict.tier is CommandTier.RISKY, command


@pytest.mark.asyncio
async def test_gate_auto_approves_declared_prefix(tmp_path):
    gate = BashCallGate(
        approver=AllowAllApprover(),
        allowed_prefixes=(str(tmp_path),),
        cwd=str(tmp_path),
        declared_safe_prefixes=("mytool list",),
    )
    outcome = await gate.check(command="mytool list", reason="列出可用项")
    assert outcome.allowed is True
    assert outcome.source == "gate"
    assert outcome.tier is CommandTier.SAFE


@pytest.mark.asyncio
async def test_adapter_wires_cli_policy_and_run_tmp_dir(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = tmp_path / "trae_config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    run_tmp_root = tmp_path / "run-tmp"
    settings = TraeRuntimeSettings(
        config_path=config_path,
        provider="test",
        model="test-model",
        model_base_url=None,
        api_key="key",
        max_steps=3,
        workspace_roots=(workspace.resolve(),),
        skills_root=tmp_path / "skills",
        run_tmp_root=run_tmp_root,
    )
    run_id = uuid4()
    request = SimpleNamespace(
        context_bundle={
            "cli_policy": {
                "tenant_id": "acme",
                "allowed_safe_prefixes": ["mytool list"],
            }
        },
        workspace_ref=str(workspace),
        tool_policy={},
        run_id=run_id,
    )

    class _FakeToolCaller:
        async def sequential_tool_call(self, calls):
            return []

        async def parallel_tool_call(self, calls):
            return []

        async def close_tools(self):
            return None

    class _FakeAgent:
        def __init__(self):
            self.tools = [SimpleNamespace(name="bash")]
            self._tool_caller = _FakeToolCaller()
            self._system_prompt = ""

        async def initialise_mcp(self):
            return None

    def factory(runtime_settings, request, trajectory):
        return SimpleNamespace(agent=_FakeAgent())

    adapter = TraeExecutionAdapter(
        request,
        emit=lambda event_type, payload=None: None,
        on_waiting=lambda interaction, batch, next_step: None,
        settings=settings,
        agent_factory=factory,
    )
    await adapter._initialize_agent()

    tmp_dir = run_tmp_root / str(run_id)
    assert tmp_dir.is_dir()
    gate = adapter._bash_gate()
    assert gate.declared_safe_prefixes == ("mytool list",)
    assert str(tmp_dir) in gate.allowed_prefixes
    assert str(workspace.resolve()) in gate.allowed_prefixes
