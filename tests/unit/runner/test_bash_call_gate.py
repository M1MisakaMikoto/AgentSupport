"""Per-call gate: reason requirement, tier routing and mode behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from session_runner.security import (
    BashCallGate,
    CommandApproval,
    CommandTier,
    MISSING_REASON,
)


class _Approver:
    def __init__(self, allowed: bool, rationale: str = "judged"):
        self.allowed = allowed
        self.rationale = rationale
        self.requests = []

    async def approve(self, request):
        self.requests.append(request)
        return CommandApproval(self.allowed, self.rationale, "llm")


@pytest.fixture
def gate_factory(tmp_path: Path):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "agent-skills" / "run-1"
    workspace.mkdir()
    skills.mkdir(parents=True)
    (workspace / "report.txt").write_text("data", encoding="utf-8")

    def build(approver, mode="silent"):
        return BashCallGate(
            approver=approver,
            allowed_prefixes=(str(workspace), str(skills)),
            cwd=str(workspace),
            mode=mode,
        )

    return build


async def test_missing_reason_is_rejected_before_classification(gate_factory):
    approver = _Approver(True)
    outcome = await gate_factory(approver).check(command="cat report.txt", reason="  ")

    assert outcome.allowed is False
    assert outcome.reason == MISSING_REASON
    assert approver.requests == []


async def test_safe_command_passes_without_asking_the_approver(gate_factory):
    approver = _Approver(False)
    outcome = await gate_factory(approver).check(command="cat report.txt", reason="读报告")

    assert outcome.allowed is True
    assert outcome.tier is CommandTier.SAFE
    assert approver.requests == []


async def test_blocked_command_is_rejected_without_asking_the_approver(gate_factory):
    approver = _Approver(True)
    outcome = await gate_factory(approver).check(command="rm -rf report.txt", reason="清理")

    assert outcome.allowed is False
    assert outcome.tier is CommandTier.BLOCKED
    assert approver.requests == []


async def test_silent_mode_sends_risky_commands_to_the_approver(gate_factory):
    approver = _Approver(False, "看上去在读环境")
    outcome = await gate_factory(approver).check(
        command="find . -name '*.md'", reason="找文件"
    )

    assert outcome.allowed is False
    assert outcome.tier is CommandTier.RISKY
    assert outcome.reason == "看上去在读环境"
    assert len(approver.requests) == 1
    assert approver.requests[0].mode == "silent"


async def test_default_mode_does_not_judge_risky_twice(gate_factory):
    approver = _Approver(False)
    outcome = await gate_factory(approver, mode="default").check(
        command="find . -name '*.md'", reason="找文件"
    )

    assert outcome.allowed is True
    assert outcome.source == "human"
    assert approver.requests == []


async def test_no_approval_mode_is_not_gated(gate_factory):
    approver = _Approver(False)
    outcome = await gate_factory(approver, mode="no_approval").check(
        command="rm -rf report.txt", reason=""
    )

    assert outcome.allowed is True
    assert outcome.source == "mode"
    assert approver.requests == []
