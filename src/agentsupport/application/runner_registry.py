"""In-memory Runner registry used by the inline execution mode and tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from agent_runner_contracts.registration import (
    RunnerRegistration,
    RunnerRegistrationRequest,
)


class RunnerRegistryConflict(Exception):
    """Raised when a Runner identity is already registered."""


class InMemoryRunnerRegistry:
    def __init__(self) -> None:
        self._registrations: dict[UUID, RunnerRegistration] = {}
        self._token_hashes: dict[UUID, str] = {}

    def register(self, registration: RunnerRegistrationRequest, token_hash: str) -> RunnerRegistration:
        runner_id = self._existing_runner_id(registration)
        if runner_id is not None:
            raise RunnerRegistryConflict(f"runner {runner_id} is already registered")
        entry = RunnerRegistration(
            provider=registration.provider,
            endpoint=registration.endpoint,
            version=registration.version,
            capabilities=list(registration.capabilities),
            metadata=dict(registration.metadata),
        )
        self._registrations[entry.runner_id] = entry
        self._token_hashes[entry.runner_id] = token_hash
        return entry

    def heartbeat(
        self,
        runner_id: UUID,
        status: str,
        load: int,
        *,
        capabilities: list[str] | None = None,
        at: datetime | None = None,
    ) -> RunnerRegistration | None:
        entry = self._registrations.get(runner_id)
        if entry is None:
            return None
        entry.status = status
        entry.load = load
        if capabilities is not None:
            entry.capabilities = list(capabilities)
        entry.last_heartbeat_at = at or datetime.now(UTC)
        return entry

    def deregister(self, runner_id: UUID) -> bool:
        self._token_hashes.pop(runner_id, None)
        return self._registrations.pop(runner_id, None) is not None

    def get(self, runner_id: UUID) -> RunnerRegistration | None:
        return self._registrations.get(runner_id)

    def token_hash(self, runner_id: UUID) -> str | None:
        return self._token_hashes.get(runner_id)

    def list_ready(self, capabilities: set[str] | None = None) -> list[RunnerRegistration]:
        return [
            entry
            for entry in self._registrations.values()
            if entry.status == "READY"
            and (capabilities is None or set(entry.capabilities) >= capabilities)
        ]

    def expire_stale(
        self, *, at: datetime | None = None, timeout_seconds: float
    ) -> list[UUID]:
        cutoff = (at or datetime.now(UTC)) - timedelta(seconds=timeout_seconds)
        stale = [
            runner_id
            for runner_id, entry in self._registrations.items()
            if entry.last_heartbeat_at < cutoff
        ]
        for runner_id in stale:
            self.deregister(runner_id)
        return stale

    def _existing_runner_id(self, registration: RunnerRegistrationRequest) -> UUID | None:
        for entry in self._registrations.values():
            if entry.endpoint == registration.endpoint and entry.provider == registration.provider:
                return entry.runner_id
        return None


__all__ = ["InMemoryRunnerRegistry", "RunnerRegistryConflict"]
