"""Runner registry adapters backed by the SQLAlchemy repository."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from agent_runner_contracts.registration import RunnerRegistration, RunnerRegistrationRequest

from ..application.ports import RepositoryConflict
from ..application.runner_registry import RunnerRegistryConflict
from .persistence.sqlalchemy.repository import PostgresRepository


class SqlAlchemyRunnerRegistry:
    """Runner directory persisted in PostgreSQL, safe across stateless replicas."""

    def __init__(self, repository: PostgresRepository) -> None:
        self.repository = repository

    def register(self, registration: RunnerRegistrationRequest, token_hash: str) -> RunnerRegistration:
        existing = [
            entry
            for entry in self.repository.list_ready_runner_registrations()
            if entry.provider == registration.provider and entry.endpoint == registration.endpoint
        ]
        if existing:
            raise RunnerRegistryConflict(
                f"runner {existing[0].runner_id} is already registered"
            )
        entry = RunnerRegistration(
            provider=registration.provider,
            endpoint=registration.endpoint,
            version=registration.version,
            capabilities=list(registration.capabilities),
            metadata=dict(registration.metadata),
        )
        try:
            self.repository.save_runner_registration(entry, token_hash)
        except RepositoryConflict as exc:
            raise RunnerRegistryConflict(str(exc)) from exc
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
        return self.repository.update_runner_registration(
            runner_id,
            status=status,
            load=load,
            capabilities=capabilities,
            heartbeat_at=at or datetime.now(UTC),
        )

    def deregister(self, runner_id: UUID) -> bool:
        return self.repository.delete_runner_registration(runner_id)

    def get(self, runner_id: UUID) -> RunnerRegistration | None:
        return self.repository.get_runner_registration(runner_id)

    def token_hash(self, runner_id: UUID) -> str | None:
        return self.repository.get_runner_registration_hash(runner_id)

    def list_ready(self, capabilities: set[str] | None = None) -> list[RunnerRegistration]:
        return [
            entry
            for entry in self.repository.list_ready_runner_registrations()
            if capabilities is None or set(entry.capabilities) >= capabilities
        ]

    def expire_stale(
        self, *, at: datetime | None = None, timeout_seconds: float
    ) -> list[UUID]:
        now = at or datetime.now(UTC)
        expired = self.repository.expire_runner_registrations(
            now=now, timeout_seconds=timeout_seconds
        )
        return [UUID(item) for item in expired]


__all__ = ["SqlAlchemyRunnerRegistry"]
