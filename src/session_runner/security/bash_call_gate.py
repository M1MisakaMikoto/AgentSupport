"""Per-call decision for ``bash``: reason required, tier decides the path.

Mode handling (agreed in the plan):

- ``no_approval``: the explicit high-privilege mode — no gate at all.
- ``default``: ``RISKY`` already passed human approval upstream (``bash`` is in
  ``approval_required_tools``), so it is not judged twice.
- ``silent``: unattended, so ``RISKY`` goes to the LLM approver.
"""

from __future__ import annotations

from dataclasses import dataclass

from .approval import CommandApprovalRequest, CommandApprover
from .command_gate import DEFAULT_POLICY, CommandPolicy, CommandTier, classify_command

MISSING_REASON = "bash 调用缺少 reason：请用一句话说明这次要读取什么、为什么与当前任务相关"


@dataclass(frozen=True)
class BashCallOutcome:
    allowed: bool
    tier: CommandTier | None
    reason: str
    source: str


@dataclass(frozen=True)
class BashCallGate:
    approver: CommandApprover
    allowed_prefixes: tuple[str, ...]
    cwd: str
    mode: str = "default"
    policy: CommandPolicy = DEFAULT_POLICY

    async def check(
        self,
        *,
        command: str,
        reason: str,
        run_id: str = "",
        conversation_id: str = "",
    ) -> BashCallOutcome:
        if self.mode == "no_approval":
            return BashCallOutcome(True, None, "no_approval mode is not gated", "mode")

        if not (reason or "").strip():
            return BashCallOutcome(False, None, MISSING_REASON, "gate")

        verdict = classify_command(
            command,
            allowed_prefixes=self.allowed_prefixes,
            cwd=self.cwd,
            policy=self.policy,
        )

        if verdict.tier is CommandTier.SAFE:
            return BashCallOutcome(True, verdict.tier, verdict.reason, "gate")

        if verdict.tier is CommandTier.BLOCKED:
            return BashCallOutcome(False, verdict.tier, verdict.reason, "gate")

        if self.mode != "silent":
            return BashCallOutcome(
                True, verdict.tier, "human approval already covered this call", "human"
            )

        approval = await self.approver.approve(
            CommandApprovalRequest(
                command=command,
                reason=reason,
                tier=verdict.tier,
                gate_reason=verdict.reason,
                mode=self.mode,
                cwd=self.cwd,
                allowed_prefixes=self.allowed_prefixes,
                run_id=run_id,
                conversation_id=conversation_id,
            )
        )
        return BashCallOutcome(
            approval.allowed, verdict.tier, approval.rationale, approval.source
        )
