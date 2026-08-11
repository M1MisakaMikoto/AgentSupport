from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from agent_runner_contracts.events import EventEnvelope

from ..adapters.persistence.sqlalchemy.repository import PostgresRepository
from ..adapters.skills.local import LocalSkillProvider
from ..application.ports import (
    CoreRuntime,
    RepositoryConflict,
    RuntimeDriver,
    StaleClaim,
)
from ..bootstrap.container import (
    build_core_runtime,
    build_repository,
    build_runtime_driver,
    build_skill_provider,
)
from ..bootstrap.settings import Settings, settings
from ..domain import TERMINAL_STATES, Checkpoint, ExecutionState
from ..domain.coordination import JobClaim, JobState, RunCommand, RunnerEndpoint


def _instance_id(config: Settings) -> str:
    return config.instance_id or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


@dataclass
class OwnedRun:
    claim: JobClaim
    endpoint: RunnerEndpoint


class DistributedWorker:
    """Database-coordinated execution worker safe to run with multiple replicas."""

    def __init__(
        self,
        config: Settings = settings,
        *,
        repository: PostgresRepository | None = None,
        runtime_driver: RuntimeDriver | None = None,
        core_runtime: CoreRuntime | None = None,
        skill_provider: LocalSkillProvider | None = None,
    ) -> None:
        if config.persistence_mode != "postgres":
            raise ValueError("distributed worker requires PostgreSQL persistence mode")
        self.config = config
        self.worker_id = _instance_id(config)
        self.repository = repository or build_repository(config)
        assert self.repository is not None
        self.runtime_driver = runtime_driver or build_runtime_driver(config)
        self.core_runtime = core_runtime if core_runtime is not None else build_core_runtime(config)
        self.skill_provider = skill_provider or build_skill_provider(config)
        self.enabled_skills = [
            item.strip() for item in config.enabled_skills.split(",") if item.strip()
        ]
        self.owned: dict[UUID, OwnedRun] = {}
        self._starting: set[asyncio.Task[None]] = set()

    def _workspace_ref(self, workspace_path: str, dynamic_endpoint: str | None) -> str:
        if dynamic_endpoint and not self.config.core_runner_url:
            return "/workspace"
        if self.config.core_runner_workspace_root:
            return str(
                PurePosixPath(
                    "/"
                    + str(self.config.core_runner_workspace_root)
                    .replace("\\", "/")
                    .lstrip("/")
                )
                / Path(workspace_path).name
            )
        return "/workspace"

    async def _heartbeat(self, claim: JobClaim, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.config.job_heartbeat_seconds
                )
            except TimeoutError:
                self.repository.renew_job_claim(
                    claim.job.run_id,
                    self.worker_id,
                    claim.claim_token,
                    lease_seconds=self.config.job_lease_seconds,
                )

    async def _with_heartbeat(self, claim: JobClaim, operation):
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(claim, stop))
        try:
            return await operation
        finally:
            stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    def _append_runner_event(self, claim: JobClaim, event: EventEnvelope) -> EventEnvelope:
        return self.repository.append_claimed_event(
            claim.job.run_id,
            self.worker_id,
            claim.claim_token,
            event.type,
            event.payload,
            source=event.source,
            runner_seq=event.seq,
            event_id=event.event_id,
        )

    async def _forward_response(self, claim: JobClaim, response: dict[str, Any]) -> None:
        for raw_event in response.get("events", []):
            self._append_runner_event(claim, EventEnvelope.model_validate(raw_event))

    async def _checkpoint_waiting(self, claim: JobClaim) -> Checkpoint | None:
        if self.core_runtime is None:
            return None
        checkpoint = await self.core_runtime.checkpoint(
            claim.job.run_id, "distributed_waiting_input"
        )
        self.repository.save_claimed_checkpoint(
            checkpoint,
            self.worker_id,
            claim.claim_token,
            reason="distributed_waiting_input",
        )
        return checkpoint

    async def _stop_and_finish(
        self,
        owned: OwnedRun,
        state: JobState,
        error: dict[str, Any] | None = None,
    ) -> None:
        await self.runtime_driver.stop(owned.endpoint.runtime_id)
        self.repository.finish_execution_job(
            owned.claim.job.run_id,
            self.worker_id,
            owned.claim.claim_token,
            state,
            error=error,
        )
        self.owned.pop(owned.claim.job.run_id, None)
        if self.core_runtime is not None:
            unregister = getattr(self.core_runtime, "unregister_run_endpoint", None)
            if unregister:
                unregister(owned.claim.job.run_id)

    async def _adopt(self, claim: JobClaim) -> bool:
        endpoint = self.repository.get_runner_endpoint(claim.job.run_id)
        if endpoint is None or self.core_runtime is None:
            return False
        register = getattr(self.core_runtime, "register_run_endpoint", None)
        if register:
            register(claim.job.run_id, endpoint.endpoint)
        try:
            health = await self.core_runtime.health(claim.job.run_id)
        except Exception:  # noqa: BLE001 - failed adoption falls back to replacement
            return False
        if health.get("live", {}).get("status") != "ok":
            return False
        endpoint = self.repository.adopt_runner(claim, self.worker_id)
        self.owned[claim.job.run_id] = OwnedRun(claim, endpoint)
        return True

    async def _execute_claim(self, claim: JobClaim) -> None:
        runtime_id: str | None = None
        if claim.previous_state in {JobState.RUNNING, JobState.WAITING}:
            if await self._adopt(claim):
                return
            stale_endpoint = self.repository.get_runner_endpoint(claim.job.run_id)
            if stale_endpoint:
                await self.runtime_driver.stop(stale_endpoint.runtime_id, force=True)
        session = self.repository.get_session(claim.job.session_id)
        workspace = self.repository.get_workspace(claim.job.workspace_id)
        conversation = self.repository.get_conversation(claim.job.conversation_id)
        if session is None or workspace is None or conversation is None:
            self.repository.finish_execution_job(
                claim.job.run_id,
                self.worker_id,
                claim.claim_token,
                JobState.FAILED,
                error={"code": "RESOURCE_NOT_FOUND"},
            )
            return
        skills = (
            session.config.enabled_skill_ids()
            if session.config is not None and not session.config.is_empty()
            else self.enabled_skills
        )
        session_tool_policy = (
            session.config.tool_policy_dict()
            if session.config is not None and not session.config.is_empty()
            else {}
        )
        resuming = conversation.run.state == ExecutionState.RESUMING
        try:
            self.repository.append_claimed_event(
                claim.job.run_id,
                self.worker_id,
                claim.claim_token,
                "run.started",
                {"session_id": str(session.id)},
            )
            runtime_id = await asyncio.wait_for(
                self.runtime_driver.start(
                    session.id,
                    workspace.root_path,
                    claim.fence_epoch,
                    workspace.id,
                    self.skill_provider.read_only_mounts(skills),
                ),
                timeout=self.config.runtime_start_timeout_seconds,
            )
            dynamic_endpoint = await self.runtime_driver.endpoint(runtime_id)
            endpoint_url = dynamic_endpoint or self.config.core_runner_url or "memory://runner"
            endpoint = self.repository.register_runner(
                claim, self.worker_id, runtime_id, endpoint_url
            )
            owned = OwnedRun(claim, endpoint)
            self.owned[claim.job.run_id] = owned
            self.repository.append_claimed_event(
                claim.job.run_id,
                self.worker_id,
                claim.claim_token,
                "run.running",
                {"runtime_id": runtime_id},
            )
            if self.core_runtime is None:
                self.repository.append_claimed_event(
                    claim.job.run_id,
                    self.worker_id,
                    claim.claim_token,
                    "run.completed",
                    {"result": {"status": "completed"}},
                )
                await self._stop_and_finish(owned, JobState.COMPLETED)
                return
            register = getattr(self.core_runtime, "register_run_endpoint", None)
            if register:
                register(claim.job.run_id, endpoint_url)
            workspace_ref = self._workspace_ref(workspace.root_path, dynamic_endpoint)
            tool_policy = session_tool_policy or {
                "allowed_tools": [
                    "bash",
                    "str_replace_based_edit_tool",
                    "json_edit_tool",
                    "sequentialthinking",
                    "task_done",
                ],
                "approval_required_tools": [
                    "bash",
                    "str_replace_based_edit_tool",
                    "json_edit_tool",
                ],
            }

            async def sink(event: EventEnvelope) -> None:
                self._append_runner_event(claim, event)

            if resuming:
                checkpoint = (
                    self.repository.get_checkpoint(conversation.run.checkpoint_id)
                    if conversation.run.checkpoint_id
                    else None
                )
                commands = self.repository.claim_pending_commands(
                    claim.job.run_id, self.worker_id, claim.claim_token, limit=1
                )
                if checkpoint is None or not commands:
                    raise RuntimeError("paused Run cannot resume without checkpoint and command")
                command = commands[0]
                if command.type == "input":
                    event_type = "interaction.input"
                    value = command.payload.get("value")
                elif command.type == "approval":
                    event_type = "approval.decided"
                    value = command.payload.get("decision")
                else:
                    raise RuntimeError(f"unsupported resume command: {command.type}")
                self.repository.append_claimed_event(
                    claim.job.run_id,
                    self.worker_id,
                    claim.claim_token,
                    event_type,
                    command.payload,
                    event_id=command.id,
                )
                await self._with_heartbeat(
                    claim,
                    self.core_runtime.resume(
                        checkpoint,
                        value,
                        sink,
                        command_id=command.id,
                        runtime_context={
                            "session_id": str(session.id),
                            "container_id": runtime_id,
                            "lease_epoch": claim.fence_epoch,
                            "fence_epoch": claim.fence_epoch,
                            "correlation_id": uuid4().hex,
                        },
                    ),
                )
                self.repository.finish_command(command.id, self.worker_id, succeeded=True)
                persisted = self.repository.get_conversation(conversation.id)
                if persisted and persisted.run.state == ExecutionState.WAITING_INPUT:
                    await self._with_heartbeat(claim, self._checkpoint_waiting(claim))
                    return
                if persisted and persisted.run.state in TERMINAL_STATES:
                    terminal = {
                        ExecutionState.COMPLETED: JobState.COMPLETED,
                        ExecutionState.CANCELLED: JobState.CANCELLED,
                    }.get(persisted.run.state, JobState.FAILED)
                    await self._stop_and_finish(owned, terminal)
                    return
                raise RuntimeError("resumed Run returned without a durable state transition")

            request = {
                "run_id": str(claim.job.run_id),
                "conversation_id": str(conversation.id),
                "session_id": str(session.id),
                "container_id": runtime_id,
                "lease_epoch": claim.fence_epoch,
                "fence_epoch": claim.fence_epoch,
                "correlation_id": uuid4().hex,
                "context_bundle": {
                    "task": conversation.task,
                    "conversation_id": str(conversation.id),
                    "workspace_ref": workspace_ref,
                    "recent_events": [
                        event.model_dump(mode="json")
                        for event in self.repository.list_events(conversation.id)
                    ],
                    "skill_manifest": self.skill_provider.manifest(skills),
                    "tool_policy": tool_policy,
                    "mcp_refs": [],
                },
                "workspace_ref": workspace_ref,
                "tool_policy": tool_policy,
                "core_version": "0.1.0",
                "runner_url": endpoint_url,
            }
            await self._with_heartbeat(claim, self.core_runtime.run(request, sink))
            persisted = self.repository.get_conversation(conversation.id)
            if persisted and persisted.run.state == ExecutionState.WAITING_INPUT:
                await self._with_heartbeat(claim, self._checkpoint_waiting(claim))
                return
            if persisted and persisted.run.state in TERMINAL_STATES:
                terminal = {
                    ExecutionState.COMPLETED: JobState.COMPLETED,
                    ExecutionState.CANCELLED: JobState.CANCELLED,
                }.get(persisted.run.state, JobState.FAILED)
                await self._stop_and_finish(owned, terminal)
        except StaleClaim:
            if runtime_id:
                await self.runtime_driver.stop(runtime_id, force=True)
            self.owned.pop(claim.job.run_id, None)
        except Exception as exc:  # noqa: BLE001 - execution failures are durable events
            with contextlib.suppress(RepositoryConflict):
                self.repository.append_claimed_event(
                    claim.job.run_id,
                    self.worker_id,
                    claim.claim_token,
                    "run.failed",
                    {"code": "WORKER_EXECUTION_ERROR", "message": str(exc)},
                )
            if runtime_id:
                await self.runtime_driver.stop(runtime_id, force=True)
            with contextlib.suppress(RepositoryConflict):
                self.repository.finish_execution_job(
                    claim.job.run_id,
                    self.worker_id,
                    claim.claim_token,
                    JobState.RETRY,
                    error={"message": str(exc)},
                )
            self.owned.pop(claim.job.run_id, None)

    async def run_once(self) -> bool:
        claim = self.repository.claim_next_job(
            self.worker_id,
            capacity=self.config.max_active_sessions,
            lease_seconds=self.config.job_lease_seconds,
        )
        if claim is None:
            return False
        await self._execute_claim(claim)
        return True

    async def _apply_command(self, owned: OwnedRun, command: RunCommand) -> None:
        claim = owned.claim
        try:
            if self.core_runtime is None:
                raise RuntimeError("Runner is unavailable")
            if command.type == "input":
                self.repository.append_claimed_event(
                    claim.job.run_id,
                    self.worker_id,
                    claim.claim_token,
                    "interaction.input",
                    command.payload,
                    event_id=command.id,
                )
                response = await self._with_heartbeat(
                    claim,
                    self.core_runtime.accept_input(
                        claim.job.run_id,
                        str(command.payload["interaction_id"]),
                        command.payload.get("value"),
                        command_id=command.id,
                    ),
                )
            elif command.type == "approval":
                self.repository.append_claimed_event(
                    claim.job.run_id,
                    self.worker_id,
                    claim.claim_token,
                    "approval.decided",
                    command.payload,
                    event_id=command.id,
                )
                response = await self._with_heartbeat(
                    claim,
                    self.core_runtime.accept_approval(
                        claim.job.run_id,
                        str(command.payload["approval_id"]),
                        str(command.payload["decision"]),
                        command_id=command.id,
                    ),
                )
            elif command.type == "cancel":
                response = await self._with_heartbeat(
                    claim,
                    self.core_runtime.cancel(claim.job.run_id, command_id=command.id),
                )
            else:
                raise RuntimeError(f"unknown run command: {command.type}")
            await self._forward_response(claim, response)
            self.repository.finish_command(command.id, self.worker_id, succeeded=True)
            conversation = self.repository.get_conversation(claim.job.conversation_id)
            if conversation and conversation.run.state in TERMINAL_STATES:
                state = {
                    ExecutionState.COMPLETED: JobState.COMPLETED,
                    ExecutionState.CANCELLED: JobState.CANCELLED,
                }.get(conversation.run.state, JobState.FAILED)
                await self._stop_and_finish(owned, state)
            elif conversation and conversation.run.state == ExecutionState.WAITING_INPUT:
                await self._checkpoint_waiting(claim)
        except Exception:
            with contextlib.suppress(RepositoryConflict):
                self.repository.finish_command(command.id, self.worker_id, succeeded=False)
            raise

    async def maintain_owned_once(self) -> int:
        maintained = 0
        for run_id, owned in list(self.owned.items()):
            try:
                self.repository.renew_job_claim(
                    run_id,
                    self.worker_id,
                    owned.claim.claim_token,
                    lease_seconds=self.config.job_lease_seconds,
                )
                commands = self.repository.claim_pending_commands(
                    run_id, self.worker_id, owned.claim.claim_token
                )
                for command in commands:
                    await self._apply_command(owned, command)
                maintained += 1
            except StaleClaim:
                self.owned.pop(run_id, None)
            except Exception:  # noqa: BLE001 - durable command is retried on the next loop
                # The command was returned to PENDING; the next loop retries it.
                maintained += 1
        return maintained

    async def serve_forever(self) -> None:
        while True:
            self._starting = {task for task in self._starting if not task.done()}
            while len(self._starting) < self.config.max_active_sessions:
                claim = self.repository.claim_next_job(
                    self.worker_id,
                    capacity=self.config.max_active_sessions,
                    lease_seconds=self.config.job_lease_seconds,
                )
                if claim is None:
                    break
                task = asyncio.create_task(self._execute_claim(claim))
                self._starting.add(task)
            await self.maintain_owned_once()
            await asyncio.sleep(self.config.worker_poll_interval_seconds)


def main() -> None:
    asyncio.run(DistributedWorker().serve_forever())


if __name__ == "__main__":
    main()
