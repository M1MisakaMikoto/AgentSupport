"""Approval layer: fail-closed behaviour, verdict parsing and per-run caching."""

from __future__ import annotations

import asyncio

from session_runner.security import (
    CachingApprover,
    CommandApprovalRequest,
    CommandTier,
    DenyAllApprover,
    LlmCommandApprover,
)


def _request(command: str = "find /workspace -name '*.md'", reason: str = "定位报告文件") -> CommandApprovalRequest:
    return CommandApprovalRequest(
        command=command,
        reason=reason,
        tier=CommandTier.RISKY,
        gate_reason="find is not on the read-only whitelist",
        mode="silent",
        cwd="/workspace-data/session-1",
        allowed_prefixes=("/workspace-data/session-1", "/opt/agent-skills/run-1"),
        run_id="run-1",
        conversation_id="conv-1",
    )


class _Judge:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


async def test_deny_all_is_the_default_fail_closed_behaviour():
    verdict = await DenyAllApprover().approve(_request())

    assert verdict.allowed is False
    assert verdict.source == "deny-all"


async def test_prompt_carries_restriction_command_and_reason():
    judge = _Judge(['{"allow": true, "rationale": "只读定位"}'])
    request = _request()

    verdict = await LlmCommandApprover(judge).approve(request)

    assert verdict.allowed is True
    _, user = judge.calls[0]
    assert request.command in user
    assert request.reason in user
    assert "/opt/agent-skills/run-1" in user
    assert "silent" in user


async def test_unparsable_verdict_denies():
    judge = _Judge(["我觉得可以放行"])

    verdict = await LlmCommandApprover(judge).approve(_request())

    assert verdict.allowed is False
    assert "not parsable" in verdict.rationale


async def test_fenced_json_is_accepted():
    judge = _Judge(['```json\n{"allow": false, "rationale": "会写盘"}\n```'])

    verdict = await LlmCommandApprover(judge).approve(_request())

    assert verdict.allowed is False
    assert verdict.rationale == "会写盘"


async def test_judge_exception_denies():
    judge = _Judge([RuntimeError("model down")])

    verdict = await LlmCommandApprover(judge).approve(_request())

    assert verdict.allowed is False
    assert "approval failed" in verdict.rationale


async def test_timeout_denies():
    class _SlowJudge:
        async def complete(self, *, system: str, user: str) -> str:
            await asyncio.sleep(0.2)
            return '{"allow": true, "rationale": "too late"}'

    verdict = await LlmCommandApprover(_SlowJudge(), timeout_seconds=0.01).approve(_request())

    assert verdict.allowed is False
    assert verdict.rationale == "approval timed out"


async def test_cache_reuses_identical_requests_only():
    judge = _Judge(
        ['{"allow": true, "rationale": "ok"}', '{"allow": true, "rationale": "ok2"}']
    )
    approver = CachingApprover(LlmCommandApprover(judge))

    first = await approver.approve(_request())
    second = await approver.approve(_request())
    third = await approver.approve(_request(reason="换个理由"))

    assert first.allowed is True and second.allowed is True and third.allowed is True
    assert second.rationale.endswith("(cached)")
    assert len(judge.calls) == 2
