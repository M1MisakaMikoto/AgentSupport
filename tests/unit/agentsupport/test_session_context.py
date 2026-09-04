"""Session-wide context: later conversations see earlier dialogue history."""

from __future__ import annotations

from _support import make_temporal_service as _make_service


async def test_second_conversation_sees_first_round_dialogue(tmp_path):
    env = _make_service(tmp_path)
    service = env.service

    workspace = service.create_workspace("ctx")
    session = service.create_session(workspace.id, tenant_id="t-1")
    first = await service.create_conversation(
        session.id, "记住规则：收到 /close 就回复 /close"
    )
    await env.coordinator.wait_for_run(str(first.run.run_id), timeout_seconds=15)
    second = await service.create_conversation(
        session.id, "/close", parent_conversation_id=first.id
    )
    await env.coordinator.wait_for_run(str(second.run.run_id), timeout_seconds=15)

    assert len(env.fake.captured) == 2
    first_events = env.fake.captured[0]["context_bundle"]["recent_events"]
    second_events = env.fake.captured[1]["context_bundle"]["recent_events"]
    # second round must include the first round's instruction message
    first_task_events = [
        e
        for e in second_events
        if e["type"] == "message"
        and "记住规则" in str(e.get("payload", {}).get("content", ""))
    ]
    assert first_task_events, "second conversation context is missing the earlier instruction"
    # context is session-wide: it also carries the first conversation's completion
    assert any(e["type"] == "run.completed" for e in second_events)
    # per-conversation tool history is still present (no cross-contamination beyond limit)
    assert len(second_events) >= len(first_events)
