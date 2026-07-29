import pytest

from agent_platform.tool_gateway import (
    ApprovalDecision,
    AuthorizationStatus,
    ToolBatch,
    ToolCall,
    ToolDescriptor,
    ToolGatewayControlPlane,
    ToolGatewayPolicy,
)
from session_runner.tool_gateway import ToolGatewayExecutor


def make_executor(tmp_path, calls):
    target = tmp_path / "approved.txt"

    async def write_file(arguments):
        calls.append("write")
        target.write_text(arguments["content"], encoding="utf-8")
        return str(target)

    async def audit(arguments):
        calls.append("audit")
        return arguments["message"]

    control = ToolGatewayControlPlane(
        [
            ToolDescriptor(name="workspace.write", requires_approval=True),
            ToolDescriptor(name="audit.record"),
        ]
    )
    policy = ToolGatewayPolicy(allowed_tools={"workspace.write", "audit.record"})
    executor = ToolGatewayExecutor(
        control, policy, {"workspace.write": write_file, "audit.record": audit}
    )
    batch = ToolBatch(
        calls=[
            ToolCall(call_id="call-write", name="workspace.write", arguments={"content": "done"}),
            ToolCall(call_id="call-audit", name="audit.record", arguments={"message": "recorded"}),
        ]
    )
    return executor, batch, target


@pytest.mark.asyncio
async def test_approval_happens_before_batch_side_effects_and_calls_execute_once(tmp_path):
    calls = []
    executor, batch, target = make_executor(tmp_path, calls)
    authorization = executor.authorize(batch)
    assert authorization.status == AuthorizationStatus.REQUIRES_APPROVAL
    with pytest.raises(RuntimeError, match="requires approval"):
        await executor.execute(batch, authorization)
    assert not target.exists()
    assert calls == []

    checkpoint = executor.checkpoint(batch, authorization)
    restored_batch, restored_authorization = executor.restore(checkpoint)
    first = await executor.execute(
        restored_batch, restored_authorization, ApprovalDecision.APPROVE_ONCE
    )
    second = await executor.execute(
        restored_batch, restored_authorization, ApprovalDecision.APPROVE_ONCE
    )
    assert all(result.success for result in first + second)
    assert target.read_text(encoding="utf-8") == "done"
    assert calls == ["write", "audit"]


@pytest.mark.asyncio
async def test_rejected_batch_has_no_side_effects_and_tampering_is_rejected(tmp_path):
    calls = []
    executor, batch, target = make_executor(tmp_path, calls)
    authorization = executor.authorize(batch)
    rejected = await executor.execute(batch, authorization, ApprovalDecision.REJECT)
    assert all(not result.success for result in rejected)
    assert calls == []
    assert not target.exists()

    checkpoint = executor.checkpoint(batch, authorization)
    checkpoint["tool_batch"]["calls"][0]["arguments"]["content"] = "tampered"
    with pytest.raises(ValueError, match="batch hash"):
        executor.restore(checkpoint)
