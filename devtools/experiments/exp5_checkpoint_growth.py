"""Experiment 5: checkpoint stores the full event history every time.

The runner checkpoint endpoint serializes ``state.events`` into
``context_bundle.recent_events``. With N events and K checkpoints the stored
payload grows as K x N because every checkpoint duplicates the whole history.
This experiment measures ``recent_events`` length and payload size for
different batch sizes before and after the bounded-window fix.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import uuid4

import httpx
from common import record_evidence, table

from agent_runner_contracts.execution import RunRequest
from session_runner.serving.http.app import create_runner_app


def _run_request(batch_size: int) -> RunRequest:
    calls = [
        {"call_id": f"call-{i}", "name": "echo", "arguments": {"value": i}}
        for i in range(batch_size)
    ]
    tool_policy = {
        "tools": [{"name": "echo"}],
        "allowed_tools": ["echo"],
        "approval_required_tools": [],
    }
    return RunRequest(
        run_id=uuid4(),
        conversation_id=uuid4(),
        session_id=uuid4(),
        container_id="experiment",
        lease_epoch=1,
        fence_epoch=1,
        correlation_id="exp5",
        context_bundle={
            "task": "checkpoint growth",
            "conversation_id": str(uuid4()),
            "workspace_ref": "/workspace",
            "recent_events": [],
            "skill_manifest": [],
            "skills": [],
            "mcp_refs": [],
            "tool_policy": tool_policy,
            "tool_batch": {"calls": calls},
        },
        workspace_ref="/workspace",
        tool_policy=tool_policy,
        core_version="0.1.0",
    )


async def measure(batch_size: int) -> dict[str, object]:
    runner_app = create_runner_app(runner_mode="deterministic", registration_client=None)
    transport = httpx.ASGITransport(app=runner_app)
    request = _run_request(batch_size)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://runner", timeout=30.0
    ) as client:
        start = await client.post("/runs", json=request.model_dump(mode="json"))
        start.raise_for_status()
        events = start.json()["events"]
        checkpoint = await client.post(
            f"/runs/{request.run_id}/checkpoint", json={"reason": "experiment"}
        )
        checkpoint.raise_for_status()
        payload = checkpoint.json()
        recent = payload["context_bundle"]["recent_events"]
        return {
            "batch_size": batch_size,
            "run_events": len(events),
            "recent_events_len": len(recent),
            "checkpoint_bytes": len(json.dumps(payload)),
            "recent_events_bytes": len(json.dumps(recent)),
        }


async def main(phase: str) -> str:
    rows: list[list[object]] = []
    for batch_size in (200, 800):
        result = await measure(batch_size)
        rows.append(
            [
                batch_size,
                result["run_events"],
                result["recent_events_len"],
                f"{result['recent_events_bytes'] / 1024:.1f} KiB",
                f"{result['checkpoint_bytes'] / 1024:.1f} KiB",
            ]
        )
    return "\n".join(
        [
            "## Checkpoint payload vs event count",
            table(
                rows,
                [
                    "tool calls",
                    "run events",
                    "recent_events stored",
                    "recent_events size",
                    "full checkpoint size",
                ],
            ),
            "",
            "Expected before fix: `recent_events stored == run events` and size grows",
            "linearly with history. Expected after fix: `recent_events stored` is",
            "capped to a bounded window while `run events` still grows.",
        ]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    markdown = asyncio.run(main(args.phase))
    record_evidence(
        "exp5_checkpoint_growth",
        args.phase,
        markdown,
        command=f"python devtools/experiments/exp5_checkpoint_growth.py --phase {args.phase}",
    )
