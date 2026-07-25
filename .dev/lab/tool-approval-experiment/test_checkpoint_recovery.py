"""Deterministic contract probe for platform-owned pause and resume."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path


def context_hash(context: dict[str, object]) -> str:
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class FakeCore:
    def __init__(self, context: dict[str, object], events: list[dict[str, object]]) -> None:
        self.context = context
        self.events = events
        self.pending = {"interaction_id": "question-1", "kind": "user_input"}

    def run(self) -> str:
        self.events.append({"type": "run.started"})
        self.events.append({"type": "interaction.requested", "payload": self.pending})
        return "WAITING_INPUT"

    def checkpoint(self) -> dict[str, object]:
        return {
            "checkpoint_id": "checkpoint-1",
            "context": self.context,
            "context_hash": context_hash(self.context),
            "pending_interaction": self.pending,
            "last_event_seq": len(self.events),
        }

    @classmethod
    def resume(
        cls,
        checkpoint: dict[str, object],
        input_value: str,
        events: list[dict[str, object]],
        marker: Path,
    ) -> str:
        context = checkpoint["context"]
        assert isinstance(context, dict)
        assert checkpoint["context_hash"] == context_hash(context)
        assert checkpoint["pending_interaction"] == {
            "interaction_id": "question-1",
            "kind": "user_input",
        }
        events.append({"type": "interaction.accepted", "payload": {"value": input_value}})
        assert not marker.exists(), "resume must not duplicate a pre-checkpoint side effect"
        events.append({"type": "tool.call", "payload": {"call_id": "call-1"}})
        marker.write_text(input_value, encoding="utf-8")
        events.append({"type": "tool.result", "payload": {"call_id": "call-1", "success": True}})
        events.append({"type": "run.completed"})
        return "COMPLETED"


def main() -> None:
    events: list[dict[str, object]] = []
    context = {"task": "ask then write", "workspace_ref": "/workspace"}

    with tempfile.TemporaryDirectory(prefix="checkpoint-recovery-") as directory:
        root = Path(directory)
        marker = root / "marker.txt"
        first_core = FakeCore(context, events)
        assert first_core.run() == "WAITING_INPUT"
        checkpoint = first_core.checkpoint()
        checkpoint_path = root / "checkpoint.json"
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

        # The first process is gone. A new process resumes only from platform data.
        restored = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert FakeCore.resume(restored, "USER-ANSWER", events, marker) == "COMPLETED"
        assert marker.read_text(encoding="utf-8") == "USER-ANSWER"
        assert [event["type"] for event in events] == [
            "run.started",
            "interaction.requested",
            "interaction.accepted",
            "tool.call",
            "tool.result",
            "run.completed",
        ]

        tampered = dict(restored)
        tampered["context_hash"] = "stale"
        try:
            FakeCore.resume(tampered, "SHOULD-FAIL", events, marker)
        except AssertionError:
            pass
        else:
            raise AssertionError("stale checkpoint must be rejected")

    print(json.dumps({
        "platform_owned_checkpoint": True,
        "cross_process_resume": True,
        "no_duplicate_side_effect": True,
        "stale_context_rejected": True,
        "event_count": 6,
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
