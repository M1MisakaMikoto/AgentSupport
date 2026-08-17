import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agentsupport.config import Settings
from agentsupport.domain import Checkpoint, ContextBundle
from agentsupport.services import AgentSupportService


def _make_checkpoint(conversation_id, created_at):
    context = ContextBundle(
        task="task",
        conversation_id=conversation_id,
        workspace_ref="/workspace",
    )
    digest = hashlib.sha256(
        json.dumps(
            context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return Checkpoint(
        conversation_id=conversation_id,
        run_id=uuid4(),
        last_event_seq=1,
        context_bundle=context,
        workspace_ref="/workspace",
        workspace_write_lease_epoch=0,
        context_bundle_hash=digest,
        created_at=created_at,
    )


@pytest.fixture
def service(tmp_path):
    return AgentSupportService(Settings(workspace_root=tmp_path / "workspaces"))


def test_settings_expose_retention_windows(tmp_path):
    settings = Settings(workspace_root=tmp_path)
    assert settings.retention_idempotency_hours == 24
    assert settings.retention_unreferenced_checkpoints_hours == 24
    assert settings.retention_terminal_events_days == 90


def test_prune_removes_expired_idempotency_records(service):
    first = service.create_workspace("demo", idempotency_key="k1")
    service.create_workspace("other", idempotency_key="k2")
    service.idempotency[("workspace", "k1")].created_at = datetime.now(UTC) - timedelta(hours=25)

    result = service.prune_retained_state()

    assert result["idempotency_records"] == 1
    assert ("workspace", "k1") not in service.idempotency
    assert ("workspace", "k2") in service.idempotency
    recreated = service.create_workspace("demo", idempotency_key="k1")
    assert recreated.id != first.id


def test_prune_removes_only_unreferenced_old_checkpoints(service):
    conversation_id = uuid4()
    old = _make_checkpoint(conversation_id, datetime.now(UTC) - timedelta(hours=25))
    recent = _make_checkpoint(conversation_id, datetime.now(UTC))
    service.checkpoints[old.checkpoint_id] = old
    service.checkpoints[recent.checkpoint_id] = recent

    result = service.prune_retained_state()

    assert result["unreferenced_checkpoints"] == 1
    assert old.checkpoint_id not in service.checkpoints
    assert recent.checkpoint_id in service.checkpoints


@pytest.mark.asyncio
async def test_prune_keeps_checkpoint_referenced_by_conversation(service):
    workspace = service.create_workspace("demo")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "task")
    old = _make_checkpoint(conversation.id, datetime.now(UTC) - timedelta(hours=25))
    conversation.run.checkpoint_id = old.checkpoint_id
    service.checkpoints[old.checkpoint_id] = old

    result = service.prune_retained_state()

    assert result["unreferenced_checkpoints"] == 0
    assert old.checkpoint_id in service.checkpoints
