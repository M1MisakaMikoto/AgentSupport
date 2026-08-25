"""Runner self-registration, heartbeats, selection and session supervision."""



from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agent_runner_contracts.registration import (
    RunnerHeartbeat,
    RunnerRegistration,
    RunnerRegistrationRequest,
    RunnerRegistrationResponse,
)

from ..domain import (
    ExecutionState,
)
from ..observability import metrics as obs_metrics
from .common import (
    ServiceError,
    _hash_token,
)
from .runner_registry import RunnerRegistryConflict


class RunnerOpsMixin:

    def register_runner(
        self, request: RunnerRegistrationRequest, *, bootstrap_token: str | None
    ) -> RunnerRegistrationResponse:
        if not self.config.runner_token:
            raise ServiceError(
                "RUNNER_REGISTRATION_DISABLED",
                "runner registration is not configured; set AGENTSUPPORT_RUNNER_TOKEN",
                503,
            )
        if not bootstrap_token or not secrets.compare_digest(
            bootstrap_token, self.config.runner_token
        ):
            raise ServiceError("RUNNER_TOKEN_INVALID", "invalid runner token", 401)
        runner_token = secrets.token_urlsafe(32)
        try:
            entry = self.runner_registry.register(request, _hash_token(runner_token))
        except RunnerRegistryConflict as exc:
            raise ServiceError("RUNNER_ALREADY_REGISTERED", str(exc), 409) from exc
        return RunnerRegistrationResponse(
            runner_id=entry.runner_id, token=runner_token
        )


    def _verify_runner_token(self, runner_id: UUID, runner_token: str | None) -> None:
        if not runner_token:
            raise ServiceError("RUNNER_TOKEN_INVALID", "missing runner token", 401)
        stored = self.runner_registry.token_hash(runner_id)
        if stored is None:
            raise ServiceError("RUNNER_NOT_FOUND", "runner is not registered", 404)
        if not secrets.compare_digest(_hash_token(runner_token), stored):
            raise ServiceError("RUNNER_TOKEN_INVALID", "invalid runner token", 401)


    def runner_heartbeat(
        self, runner_id: UUID, payload: RunnerHeartbeat, *, runner_token: str | None
    ) -> RunnerRegistration:
        self._verify_runner_token(runner_id, runner_token)
        updated = self.runner_registry.heartbeat(
            runner_id,
            payload.status,
            payload.load,
            capabilities=payload.capabilities,
        )
        if updated is None:
            raise ServiceError("RUNNER_NOT_FOUND", "runner is not registered", 404)
        return updated


    def runner_deregister(self, runner_id: UUID, *, runner_token: str | None) -> None:
        self._verify_runner_token(runner_id, runner_token)
        if not self.runner_registry.deregister(runner_id):
            raise ServiceError("RUNNER_NOT_FOUND", "runner is not registered", 404)


    def runner_snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "runner_id": str(entry.runner_id),
                "type": entry.provider,
                "version": entry.version,
                "capabilities": list(entry.capabilities),
                "status": entry.status,
            }
            for entry in self.runner_registry.list_ready()
        ]


    def select_ready_runner(self, capabilities: set[str]) -> RunnerRegistration | None:
        ready = self.runner_registry.list_ready(capabilities)
        if not ready:
            return None
        # Prefer the least-loaded Runner; ties keep registry order.
        return min(ready, key=lambda entry: entry.load)


    def prune_stale_runners(self) -> int:
        expired = self.runner_registry.expire_stale(
            at=datetime.now(UTC),
            timeout_seconds=self.config.runner_heartbeat_timeout_seconds,
        )
        if expired:
            obs_metrics.record_runner_heartbeat_expired()
        return len(expired)


    async def supervise_active_sessions(self) -> int:
        """Mark sessions LOST only after repeated failed container health checks."""

        lost = 0
        for session in list(self.sessions.values()):
            if not session.active_container_id:
                self._health_failures.pop(session.id, None)
                continue
            try:
                inspection = await self.runtime_driver.inspect(session.active_container_id)
                healthy = (
                    inspection.get("status") in {"running", "created"}
                    and session.active_run_id is not None
                )
                if healthy and self.core_runtime is not None and session.active_run_id is not None:
                    endpoint = await self._runner_endpoint(session)
                    register = getattr(self.core_runtime, "register_run_endpoint", None)
                    if register is not None:
                        register(session.active_run_id, endpoint)
                    health = await self.core_runtime.health(session.active_run_id)
                    healthy = health.get("live", {}).get("status") == "ok" and health.get(
                        "ready", {}
                    ).get("status") in {"ok", "ready"}
            except Exception:  # noqa: BLE001 - health failures are counted, not raised
                healthy = False
            if healthy:
                self._health_failures.pop(session.id, None)
                continue
            failures = self._health_failures.get(session.id, 0) + 1
            self._health_failures[session.id] = failures
            if failures < self.config.health_failure_threshold:
                continue
            if session.active_run_id is None:
                if await self._release_session(session):
                    self._health_failures.pop(session.id, None)
                    lost += 1
                continue
            conversation = next(
                (
                    item
                    for item in self.conversations.values()
                    if item.run.run_id == session.active_run_id
                ),
                None,
            )
            if conversation is None:
                continue
            conversation.run.state = ExecutionState.LOST
            self._append(
                conversation,
                "run.lost",
                {"container_id": session.active_container_id, "health_failures": failures},
            )
            await self._release_session(session, conversation)
            self._health_failures.pop(session.id, None)
            lost += 1
        return lost
