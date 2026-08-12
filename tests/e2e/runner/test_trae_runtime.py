import json
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.domain import Checkpoint, ContextBundle
from session_runner.server import create_runner_app
from session_runner.trae_runtime import TraeRuntimeSettings


@dataclass
class FakeTraeCall:
    name: str
    call_id: str
    arguments: dict
    id: str | None = None


@dataclass
class FakeTraeResult:
    call_id: str
    name: str
    success: bool
    result: str | None = None
    error: str | None = None


class FakeDelegate:
    def __init__(self, marker):
        self.marker = marker
        self.calls = 0

    async def close_tools(self):
        return None

    async def sequential_tool_call(self, calls):
        self.calls += 1
        self.marker.write_text(calls[0].arguments["value"], encoding="utf-8")
        return [
            FakeTraeResult(
                call_id=calls[0].call_id,
                name=calls[0].name,
                success=True,
                result="written",
            )
        ]

    async def parallel_tool_call(self, calls):
        return await self.sequential_tool_call(calls)


class FakeAgent:
    def __init__(self, marker, trajectory, *, fail=False):
        self.marker = marker
        self.trajectory = trajectory
        self.fail = fail
        self.delegate = FakeDelegate(marker)
        self.agent = SimpleNamespace(
            tools=[SimpleNamespace(name="workspace.write")],
            _tool_caller=self.delegate,
        )

    async def run(self, task, extra_args):
        if self.fail:
            raise RuntimeError("synthetic model failure")
        results = await self.agent._tool_caller.sequential_tool_call(
            [
                FakeTraeCall(
                    name="workspace.write",
                    call_id="write-1",
                    arguments={"value": "real-mode"},
                )
            ]
        )
        self.trajectory.write_text(
            json.dumps({"agent_steps": [{"step_number": 1}]}), encoding="utf-8"
        )
        return SimpleNamespace(
            success=results[0].success,
            final_result="real Trae completed",
            steps=[1],
        )


class FakeResumeAgent(FakeAgent):
    def __init__(self, marker, trajectory):
        super().__init__(marker, trajectory)
        self.agent.initial_messages = []
        self.agent.max_steps = 2

    def _new_task(self, task, extra_args):
        self.agent.task = task

    async def _run_llm_step(self, step, messages, execution):
        from trae_agent.agent.agent_basics import AgentState

        execution.agent_state = AgentState.COMPLETED
        execution.success = True
        execution.final_result = "resumed"
        return messages

    async def _finalize_step(self, step, messages, execution):
        execution.steps.append(step)

    async def _close_tools(self):
        await self.agent._tool_caller.close_tools()


def settings(tmp_path):
    config = tmp_path / "trae_config.yaml"
    config.write_text("agents: {}\n", encoding="utf-8")
    return TraeRuntimeSettings(
        config_path=config,
        provider="test",
        model="test-model",
        model_base_url=None,
        api_key="environment-secret",
        max_steps=3,
        workspace_roots=(tmp_path.resolve(),),
    )


def request_payload(workspace):
    return {
        "run_id": str(uuid4()),
        "conversation_id": str(uuid4()),
        "session_id": str(uuid4()),
        "container_id": "session-container",
        "lease_epoch": 1,
        "correlation_id": "real-trae-contract",
        "workspace_ref": str(workspace),
        "context_bundle": {"task": "write through real mode"},
        "tool_policy": {
            "allowed_tools": ["workspace.write"],
            "approval_required_tools": ["workspace.write"],
        },
    }


@pytest.mark.asyncio
async def test_real_mode_waits_before_side_effect_and_completes_after_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "marker.txt"
    created = []

    def factory(runtime_settings, request, trajectory):
        agent = FakeAgent(marker, trajectory)
        created.append(agent)
        return agent

    app = create_runner_app(
        runner_mode="trae",
        trae_settings=settings(tmp_path),
        trae_agent_factory=factory,
    )
    payload = request_payload(workspace)
    run_id = payload["run_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        started = await client.post("/runs", json=payload)
        assert started.json()["status"] == "WAITING_INPUT"
        assert not marker.exists()
        interaction = started.json()["events"][-1]["payload"]

        checkpoint = await client.post(f"/runs/{run_id}/checkpoint", json={"reason": "timeout"})
        assert checkpoint.json()["pending_tool_calls"][0]["call_id"] == "write-1"
        assert checkpoint.json()["tool_batch_hash"]

        approved = await client.post(
            f"/runs/{run_id}/approval",
            json={
                "approval_id": interaction["interaction_id"],
                "decision": "APPROVE_ONCE",
            },
        )

    assert approved.json()["status"] == "COMPLETED"
    assert marker.read_text(encoding="utf-8") == "real-mode"
    assert created[0].delegate.calls == 1
    event_types = [event["type"] for event in approved.json()["events"]]
    assert "tool.result" in event_types
    assert "trajectory.recorded" in event_types
    assert event_types[-1] == "run.completed"


@pytest.mark.asyncio
async def test_real_mode_records_missing_trajectory_without_hiding_completion(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class NoTrajectoryAgent(FakeAgent):
        async def run(self, task, extra_args):
            return SimpleNamespace(
                success=True,
                final_result="completed without trajectory",
                steps=[1],
            )

    app = create_runner_app(
        runner_mode="trae",
        trae_settings=settings(tmp_path),
        trae_agent_factory=lambda runtime_settings, request, trajectory: NoTrajectoryAgent(
            workspace / "unused.txt", trajectory
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        response = await client.post("/runs", json=request_payload(workspace))

    assert response.json()["status"] == "COMPLETED"
    event_types = [event["type"] for event in response.json()["events"]]
    assert "trajectory.missing" in event_types
    assert event_types[-1] == "run.completed"


@pytest.mark.asyncio
async def test_real_mode_maps_agent_failure_to_run_failed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def factory(runtime_settings, request, trajectory):
        return FakeAgent(workspace / "unused.txt", trajectory, fail=True)

    app = create_runner_app(
        runner_mode="trae",
        trae_settings=settings(tmp_path),
        trae_agent_factory=factory,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        response = await client.post("/runs", json=request_payload(workspace))

    assert response.json()["status"] == "FAILED"
    assert response.json()["events"][-1]["payload"]["code"] == "TRAE_RUNTIME_ERROR"


@pytest.mark.asyncio
async def test_real_mode_resume_rebuilds_tool_batch_in_new_agent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "resume-marker.txt"
    created = []

    def factory(runtime_settings, request, trajectory):
        agent = FakeResumeAgent(marker, trajectory)
        agent.agent.new_task = agent._new_task
        agent.agent._run_llm_step = agent._run_llm_step
        agent.agent._finalize_step = agent._finalize_step
        agent.agent._close_tools = agent._close_tools
        created.append(agent)
        return agent

    run_id = uuid4()
    context = ContextBundle(
        task="resume through Trae",
        conversation_id=uuid4(),
        workspace_ref=str(workspace),
        tool_policy={
            "allowed_tools": ["workspace.write"],
            "approval_required_tools": ["workspace.write"],
        },
    )
    import hashlib

    context_hash = hashlib.sha256(
        json.dumps(context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    checkpoint = Checkpoint(
        conversation_id=context.conversation_id,
        run_id=run_id,
        last_event_seq=3,
        context_bundle=context,
        pending_interaction={
            "interaction_id": "approval-1",
            "kind": "approval",
            "tool_batch": {
                "calls": [
                    {
                        "call_id": "resume-1",
                        "name": "workspace.write",
                        "arguments": {"value": "resumed"},
                    }
                ]
            },
            "pending_tool_calls": [
                {
                    "call_id": "resume-1",
                    "name": "workspace.write",
                    "arguments": {"value": "resumed"},
                }
            ],
            "tool_batch_hash": "",
            "tool_method": "sequential_tool_call",
            "next_step": 2,
            "tool_policy": context.tool_policy,
        },
        pending_tool_calls=[
            {
                "call_id": "resume-1",
                "name": "workspace.write",
                "arguments": {"value": "resumed"},
            }
        ],
        workspace_ref=str(workspace),
        workspace_write_lease_epoch=1,
        context_bundle_hash=context_hash,
    )
    app = create_runner_app(
        runner_mode="trae",
        trae_settings=settings(tmp_path),
        trae_agent_factory=factory,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        response = await client.post(
            f"/runs/{run_id}/resume",
            json={"checkpoint": checkpoint.model_dump(mode="json"), "value": "APPROVE_ONCE"},
        )

    assert response.json()["status"] == "COMPLETED"
    assert marker.read_text(encoding="utf-8") == "resumed"
    assert created[0].delegate.calls == 1
