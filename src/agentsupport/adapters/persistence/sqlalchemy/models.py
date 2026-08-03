from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class OrganizationRow(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserRow(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkspaceRow(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationRow(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    execution_state: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_interaction: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationEventRow(Base):
    __tablename__ = "conversation_events"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    runner_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ConversationCheckpointRow(Base):
    __tablename__ = "conversation_checkpoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    last_event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    context_bundle: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    context_bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pending_interaction: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    core_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    core_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tool_policy_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_versions_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workspace_write_lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContainerLeaseRow(Base):
    __tablename__ = "container_leases"
    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    container_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    owner_instance_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceWriteLeaseRow(Base):
    __tablename__ = "workspace_write_leases"
    workspace_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    owner_instance_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExecutionJobRow(Base):
    __tablename__ = "execution_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    claim_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeSlotRow(Base):
    __tablename__ = "runtime_slots"
    slot_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    claim_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunnerEndpointRow(Base):
    __tablename__ = "runner_endpoints"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    runtime_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_instance_id: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_runner_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunCommandRow(Base):
    __tablename__ = "run_commands"
    __table_args__ = (UniqueConstraint("run_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxEventRow(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    topic: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyKeyRow(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("scope", "key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    scope: Mapped[str] = mapped_column(String(80), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class RuntimeOperationRow(Base):
    __tablename__ = "runtime_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def create_schema(database_url: str) -> None:
    """Explicitly initialize the PostgreSQL schema; application startup is side-effect free."""

    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    if "active_run_id" not in {
        column["name"] for column in inspect(engine).get_columns("sessions")
    }:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE sessions ADD COLUMN active_run_id VARCHAR(36)"))
    if "checkpoint_id" not in {
        column["name"] for column in inspect(engine).get_columns("conversations")
    }:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE conversations ADD COLUMN checkpoint_id VARCHAR(36)")
            )


if __name__ == "__main__":
    from ....bootstrap.settings import settings

    create_schema(settings.database_url)
