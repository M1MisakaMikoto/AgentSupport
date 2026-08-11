"""Persistence tests for the Runner registration directory."""

from datetime import UTC, datetime, timedelta

import pytest

from agent_runner_contracts.registration import (
    RUNNER_CAPABILITIES,
    RunnerRegistration,
)
from agentsupport.application.ports import RepositoryConflict
from agentsupport.repository import PostgresRepository


def _registration() -> RunnerRegistration:
    return RunnerRegistration(
        provider="deterministic",
        endpoint="http://127.0.0.1:8080",
        version="0.1.0",
        capabilities=list(RUNNER_CAPABILITIES),
    )


def test_runner_registration_persists_heartbeats_and_expiry(tmp_path):
    repository = PostgresRepository(
        f"sqlite:///{tmp_path / 'registry.db'}", create_schema=True
    )
    entry = _registration()
    repository.save_runner_registration(entry, "token-hash")

    loaded = repository.get_runner_registration(entry.runner_id)
    assert loaded is not None
    assert loaded.endpoint == entry.endpoint
    assert repository.get_runner_registration_hash(entry.runner_id) == "token-hash"

    heartbeat_at = datetime.now(UTC)
    updated = repository.update_runner_registration(
        entry.runner_id,
        status="READY",
        load=2,
        capabilities=["run"],
        heartbeat_at=heartbeat_at,
    )
    assert updated is not None
    assert updated.load == 2
    assert updated.capabilities == ["run"]

    ready = repository.list_ready_runner_registrations()
    assert [item.runner_id for item in ready] == [entry.runner_id]

    stale = repository.expire_runner_registrations(
        now=heartbeat_at + timedelta(seconds=60), timeout_seconds=30
    )
    assert stale == [str(entry.runner_id)]
    assert repository.get_runner_registration(entry.runner_id) is None
    assert repository.delete_runner_registration(entry.runner_id) is False


def test_duplicate_runner_registration_conflicts(tmp_path):
    repository = PostgresRepository(
        f"sqlite:///{tmp_path / 'duplicate.db'}", create_schema=True
    )
    entry = _registration()
    repository.save_runner_registration(entry, "hash")
    with pytest.raises(RepositoryConflict):
        repository.save_runner_registration(entry, "other-hash")
