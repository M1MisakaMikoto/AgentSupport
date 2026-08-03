from uuid import uuid4

import pytest

from agentsupport.checkpoint import tool_policy_hash, tool_versions_hash, validate_checkpoint
from agentsupport.domain import Checkpoint, ContextBundle


def make_checkpoint():
    policy = {
        "allowed_tools": ["workspace.write"],
        "approval_required_tools": ["workspace.write"],
        "tools": [{"name": "workspace.write", "version": "1"}],
    }
    context = ContextBundle(
        task="task",
        conversation_id=uuid4(),
        workspace_ref="/workspace",
        tool_policy=policy,
    )
    import hashlib
    import json

    digest = hashlib.sha256(
        json.dumps(context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return Checkpoint(
        conversation_id=context.conversation_id,
        run_id=uuid4(),
        last_event_seq=1,
        context_bundle=context,
        workspace_ref="/workspace",
        workspace_write_lease_epoch=4,
        tool_policy_hash=tool_policy_hash(policy),
        tool_versions_hash=tool_versions_hash(policy["tools"]),
        context_bundle_hash=digest,
    )


def test_checkpoint_hash_and_lease_are_verified():
    checkpoint = make_checkpoint()
    validate_checkpoint(checkpoint, lease_epoch=4)
    with pytest.raises(ValueError, match="lease epoch"):
        validate_checkpoint(checkpoint, lease_epoch=5)
    checkpoint.context_bundle.task = "tampered"
    with pytest.raises(ValueError, match="context bundle"):
        validate_checkpoint(checkpoint, lease_epoch=4)


def test_checkpoint_tool_policy_and_versions_are_verified():
    checkpoint = make_checkpoint()
    validate_checkpoint(
        checkpoint,
        lease_epoch=4,
        expected_tool_policy_hash=checkpoint.tool_policy_hash,
        expected_tool_versions_hash=checkpoint.tool_versions_hash,
    )
    with pytest.raises(ValueError, match="tool policy"):
        validate_checkpoint(
            checkpoint,
            lease_epoch=4,
            expected_tool_policy_hash=tool_policy_hash(
                {"allowed_tools": [], "approval_required_tools": []}
            ),
        )
    with pytest.raises(ValueError, match="tool versions"):
        validate_checkpoint(
            checkpoint,
            lease_epoch=4,
            expected_tool_versions_hash=tool_versions_hash(
                [{"name": "workspace.write", "version": "2"}]
            ),
        )
