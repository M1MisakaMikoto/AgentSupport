"""Experiment 4: control-plane timeout leaves an orphaned runner run.

The control plane calls ``POST /runs`` once and waits for the whole segment
(``TraeCoreRunnerRuntime`` with a hard httpx timeout). When the segment outlives
the timeout, the control plane marks the conversation FAILED while the runner's
background task keeps executing. The fix cancels the runner before marking the
run failed; this script verifies both behaviours by driving a fake slow agent.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from common import record_evidence, table

from agentsupport.adapters.notification import InMemoryEventNotifier, InMemoryEventStore
from agentsupport.adapters.runner.http import TraeCoreRunnerRuntime
from agentsupport.adapters.runtime import DockerRuntimeDriver
from agentsupport.adapters.skills import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceStorageDriver
from agentsupport.application.runner_registry import InMemoryRunnerRegistry
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from session_runner.adapters.trae import TraeRuntimeSettings
from session_runner.serving.http.app import create_runner_app


class _InnerAgent:
    tools: list = []  # noqa: RUF012 - shared placeholder for the fake agent
    _tool_caller = object()
    _system_prompt = "experiment"
    max_steps = 4
    initial_messages: list = []  # noqa: RUF012 - fake agent placeholder
    mcp_servers_config: dict = {}  # noqa: RUF012 - fake agent placeholder
    allow_mcp_servers: list = []  # noqa: RUF012 - fake agent placeholder

    def get_system_prompt(self) -> str:
        return self._system_prompt

    async def initialise_mcp(self) -> None:
        return None


class _SlowAgent:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.agent = _InnerAgent()

    async def run(self, task: str, ctx: dict) -> SimpleNamespace:
        await asyncio.sleep(self.delay)
        return SimpleNamespace(
            success=True,
            steps=[],
            final_result="done-after-timeout",
            total_tokens=None,
        )


async def main(phase: str) -> str:
    workspace_path = Path("/workspace").resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="exp4-"))
    config_path = temp_dir / "trae_config.yaml"
    config_path.write_text("model: test\n", encoding="utf-8")

    runner_settings = TraeRuntimeSettings(
        config_path=config_path,
        provider="anthropic",
        model="test-model",
        model_base_url=None,
        api_key="experiment-key",
        max_steps=4,
        workspace_roots=(workspace_path,),
    )

    def agent_factory(settings: TraeRuntimeSettings, request: object, trajectory: Path):
        del settings, request, trajectory
        return _SlowAgent(delay=3.0)

    runner_app = create_runner_app(
        runner_mode="trae",
        trae_settings=runner_settings,
        trae_agent_factory=agent_factory,
        registration_client=None,
    )
    server = uvicorn.Server(
        uvicorn.Config(runner_app, host="127.0.0.1", port=8765, log_level="warning")
    )
    server_task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("runner uvicorn did not start")
    base_url = "http://127.0.0.1:8765"

    config = Settings(
        persistence_mode="memory",
        execution_mode="inline",
        database_url="",
        auto_create_schema=False,
        core_runner_url=base_url,
        core_runner_timeout_seconds=0.5,
        workspace_root=temp_dir / "workspace-data",
        skills_root=temp_dir / "skills",
    )
    core_runtime = TraeCoreRunnerRuntime(
        base_url,
        timeout_seconds=0.5,
    )
    original_run = core_runtime.run

    async def spy_run(request: dict, event_sink) -> dict:
        run_uuid = __import__("uuid").UUID(str(request["run_id"]))
        print(
            "[exp4] POST /runs ->",
            core_runtime._run_url(run_uuid),
            "run_id=",
            request["run_id"],
            "runner_url=",
            request.get("runner_url"),
        )
        return await original_run(request, event_sink)

    core_runtime.run = spy_run  # type: ignore[method-assign]
    service = AgentSupportService(
        config,
        events_store=InMemoryEventStore(),
        event_notifier=InMemoryEventNotifier(),
        workspace_provider=LocalWorkspaceStorageDriver(config.workspace_root),
        skill_provider=LocalSkillProvider(config.skills_root),
        runtime_driver=DockerRuntimeDriver(),
        core_runtime=core_runtime,
        repository=None,
        runner_registry=InMemoryRunnerRegistry(),
    )

    workspace = service.create_workspace("exp4", None)
    session = service.create_session(workspace.id, None)
    started = asyncio.get_running_loop().time()
    conversation = await service.create_conversation(session.id, "slow task")
    control_plane_elapsed = asyncio.get_running_loop().time() - started
    run_id = conversation.run.run_id
    print("[exp4] conversation state:", conversation.run.state.value)
    print("[exp4] conversation error:", conversation.run.error)

    async def runner_events() -> list[dict]:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            response = await client.get(f"/runs/{run_id}/events")
            response.raise_for_status()
            return response.json()

    async def runner_events_retry() -> list[dict]:
        for _ in range(10):
            try:
                return await runner_events()
            except httpx.HTTPStatusError as exc:
                print(f"[exp4] runner events lookup failed: {exc.response.status_code}")
                await asyncio.sleep(0.1)
        raise RuntimeError("runner run never appeared")

    events_at_timeout = await runner_events_retry()
    await asyncio.sleep(3.2)  # let the fake agent finish its 3s work
    events_after_wait = await runner_events_retry()

    types_at_timeout = [e["type"] for e in events_at_timeout]
    types_after_wait = [e["type"] for e in events_after_wait]
    has_completed_after = "run.completed" in types_after_wait
    has_cancelled_after = "run.cancelled" in types_after_wait

    rows = [
        ["control plane conversation state after POST", conversation.run.state.value],
        ["control plane run.error", str(conversation.run.error)],
        ["control plane request wall time", f"{control_plane_elapsed:.2f}s"],
        [
            "runner events at +0.5s (timeout)",
            ", ".join(types_at_timeout) or "(none)",
        ],
        ["runner events at +3.7s (agent finished)", ", ".join(types_after_wait)],
        ["runner emitted run.completed after control plane gave up", str(has_completed_after)],
        ["runner emitted run.cancelled", str(has_cancelled_after)],
    ]

    markdown = [
        "## Orphan-run reproduction (fake agent sleeps 3s, control plane timeout 0.5s)",
        table(rows, ["observation", "value"]),
        "",
        "### event timeline",
        "```text",
        "t=0.00s  control plane POST /runs",
        "t=0.50s  control plane httpx timeout -> conversation FAILED",
        "t=3.00s  fake agent finishes",
        "t=3.70s  runner state read for evidence",
        "```",
        "",
        "### runner events after the wait",
        "```json",
        __import__("json").dumps(events_after_wait, indent=2, default=str),
        "```",
    ]
    if phase == "after":
        assert not has_completed_after, "runner kept working after control plane gave up"
        assert has_cancelled_after, "runner was not cancelled after timeout"

    try:
        shutil.rmtree(workspace_path, ignore_errors=True)
    finally:
        server.should_exit = True
        await server_task
        shutil.rmtree(temp_dir, ignore_errors=True)
    return "\n".join(markdown)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    markdown = asyncio.run(main(args.phase))
    record_evidence(
        "exp4_runner_orphan",
        args.phase,
        markdown,
        command=f"python devtools/experiments/exp4_runner_orphan.py --phase {args.phase}",
    )
