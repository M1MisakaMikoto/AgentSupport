from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, create_engine, delete, func, inspect, or_, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from agent_runner_contracts.events import EventEnvelope
from agent_runner_contracts.registration import RunnerRegistration

from ....application.ports.persistence import RepositoryConflict, StaleClaim
from ....domain import (
    Checkpoint,
    ContextBundle,
    Conversation,
    ExecutionState,
    ProjectConfig,
    RunProjection,
    Session,
    Workspace,
)
from ....domain.coordination import (
    CommandState,
    ExecutionJob,
    JobClaim,
    JobState,
    OutboxNotification,
    RunCommand,
    RunnerEndpoint,
)
from .models import (
    Base,
    ContainerLeaseRow,
    ConversationCheckpointRow,
    ConversationEventRow,
    ConversationRow,
    ExecutionJobRow,
    IdempotencyKeyRow,
    OutboxEventRow,
    RunCommandRow,
    RunnerEndpointRow,
    RunnerRegistrationRow,
    RuntimeOperationRow,
    RuntimeSlotRow,
    SessionRow,
    WorkspaceRow,
    WorkspaceWriteLeaseRow,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class PostgresRepository:
    """SQLAlchemy repository for AgentSupport persistence and coordination."""

    def __init__(self, database_url: str, *, create_schema: bool = False) -> None:
        self.engine = create_engine(database_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        if create_schema:
            Base.metadata.create_all(self.engine)
            if "active_run_id" not in {
                column["name"] for column in inspect(self.engine).get_columns("sessions")
            }:
                with self.engine.begin() as connection:
                    connection.execute(
                        text("ALTER TABLE sessions ADD COLUMN active_run_id VARCHAR(36)")
                    )
            if "checkpoint_id" not in {
                column["name"] for column in inspect(self.engine).get_columns("conversations")
            }:
                with self.engine.begin() as connection:
                    connection.execute(
                        text("ALTER TABLE conversations ADD COLUMN checkpoint_id VARCHAR(36)")
                    )
            checkpoint_columns = {
                column["name"]
                for column in inspect(self.engine).get_columns("conversation_checkpoints")
            }
            for name, sql_type in {
                "core_type": "VARCHAR(40)",
                "core_version": "VARCHAR(80)",
                "tool_policy_hash": "VARCHAR(64)",
                "tool_versions_hash": "VARCHAR(64)",
            }.items():
                if name not in checkpoint_columns:
                    with self.engine.begin() as connection:
                        connection.execute(
                            text(
                                f"ALTER TABLE conversation_checkpoints ADD COLUMN {name} {sql_type}"
                            )
                        )
            idempotency_columns = {
                column["name"]
                for column in inspect(self.engine).get_columns("idempotency_keys")
            }
            if "created_at" not in idempotency_columns:
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE idempotency_keys ADD COLUMN created_at "
                            "TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"
                        )
                    )

    @contextmanager
    def transaction(self) -> Iterator[DbSession]:
        db = self.session_factory()
        try:
            with db.begin():
                yield db
        finally:
            db.close()

    def _idempotent_resource(
        self, db: DbSession, scope: str, key: str | None, request_hash: str
    ) -> UUID | None:
        if not key:
            return None
        row = db.execute(
            select(IdempotencyKeyRow).where(
                IdempotencyKeyRow.scope == scope, IdempotencyKeyRow.key == key
            )
        ).scalar_one_or_none()
        if not row:
            return None
        if row.request_hash != request_hash:
            raise RepositoryConflict("idempotency key was reused with a different request")
        return UUID(row.resource_id)

    def _lock_idempotency(self, db: DbSession, scope: str, key: str | None) -> None:
        if key and self.engine.dialect.name == "postgresql":
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"{scope}:{key}"},
            )

    def _save_idempotency(
        self,
        db: DbSession,
        scope: str,
        key: str | None,
        request_hash: str,
        resource_id: UUID,
        response_payload: dict[str, Any],
    ) -> None:
        if key:
            db.add(
                IdempotencyKeyRow(
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    resource_id=str(resource_id),
                    response_payload=response_payload,
                    created_at=_now(),
                )
            )

    def find_idempotent(self, scope: str, key: str | None, request_hash: str) -> UUID | None:
        with self.transaction() as db:
            return self._idempotent_resource(db, scope, key, request_hash)

    def remember_idempotent(
        self,
        scope: str,
        key: str,
        request_hash: str,
        resource_id: UUID,
        response_payload: dict[str, Any],
    ) -> None:
        with self.transaction() as db:
            self._lock_idempotency(db, scope, key)
            existing = self._idempotent_resource(db, scope, key, request_hash)
            if existing:
                return
            self._save_idempotency(db, scope, key, request_hash, resource_id, response_payload)

    def create_workspace(
        self,
        name: str,
        root_path: str,
        request_hash: str,
        idempotency_key: str | None,
        *,
        workspace_id: UUID | None = None,
    ) -> Workspace:
        with self.transaction() as db:
            self._lock_idempotency(db, "workspace", idempotency_key)
            existing = self._idempotent_resource(db, "workspace", idempotency_key, request_hash)
            if existing:
                return self.get_workspace(existing, db=db)
            if workspace_id is not None:
                workspace = self.get_workspace(workspace_id, db=db)
                if workspace:
                    return workspace
            workspace = (
                Workspace(id=workspace_id, name=name, root_path=root_path)
                if workspace_id is not None
                else Workspace(name=name, root_path=root_path)
            )
            db.add(
                WorkspaceRow(
                    id=str(workspace.id),
                    name=name,
                    storage_ref=root_path,
                    created_at=workspace.created_at,
                )
            )
            self._save_idempotency(
                db,
                "workspace",
                idempotency_key,
                request_hash,
                workspace.id,
                workspace.model_dump(mode="json"),
            )
            return workspace

    def get_workspace(self, workspace_id: UUID, *, db: DbSession | None = None) -> Workspace | None:
        if db is None:
            with self.transaction() as tx:
                return self.get_workspace(workspace_id, db=tx)
        row = db.get(WorkspaceRow, str(workspace_id))
        if not row:
            return None
        return Workspace(
            id=UUID(row.id), name=row.name, root_path=row.storage_ref, created_at=row.created_at
        )

    def list_workspaces(self) -> list[Workspace]:
        with self.transaction() as db:
            rows = db.execute(select(WorkspaceRow).order_by(WorkspaceRow.created_at)).scalars()
            return [
                Workspace(
                    id=UUID(row.id),
                    name=row.name,
                    root_path=row.storage_ref,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def create_session(
        self,
        workspace: Workspace,
        request_hash: str,
        idempotency_key: str | None,
        project_id: str | None = None,
        *,
        session_id: UUID | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        config: ProjectConfig | None = None,
    ) -> Session:
        with self.transaction() as db:
            self._lock_idempotency(db, "session", idempotency_key)
            existing = self._idempotent_resource(db, "session", idempotency_key, request_hash)
            if existing:
                return self.get_session(existing, db=db)
            if session_id is not None:
                session = self.get_session(session_id, db=db)
                if session:
                    return session
            session = (
                Session(
                    id=session_id,
                    workspace_id=workspace.id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    project_id=project_id,
                    metadata=metadata or {},
                    config=config,
                )
                if session_id is not None
                else Session(
                    workspace_id=workspace.id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    project_id=project_id,
                    metadata=metadata or {},
                    config=config,
                )
            )
            db.add(
                SessionRow(
                    id=str(session.id),
                    workspace_id=str(session.workspace_id),
                    tenant_id=session.tenant_id,
                    user_id=session.user_id,
                    project_id=str(session.project_id) if session.project_id else None,
                    labels=dict(session.metadata),
                    config=(
                        session.config.model_dump(mode="json")
                        if session.config is not None
                        else None
                    ),
                    lease_epoch=session.lease_epoch,
                    created_at=session.created_at,
                )
            )
            self._save_idempotency(
                db,
                "session",
                idempotency_key,
                request_hash,
                session.id,
                session.model_dump(mode="json"),
            )
            return session

    def get_session(self, session_id: UUID, *, db: DbSession | None = None) -> Session | None:
        if db is None:
            with self.transaction() as tx:
                return self.get_session(session_id, db=tx)
        row = db.get(SessionRow, str(session_id))
        if not row:
            return None
        active = db.execute(
            select(ContainerLeaseRow).where(
                ContainerLeaseRow.session_id == str(session_id),
                ContainerLeaseRow.status == "ACTIVE",
            )
        ).scalar_one_or_none()
        return Session(
            id=UUID(row.id),
            workspace_id=UUID(row.workspace_id),
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            project_id=row.project_id,
            metadata=dict(row.labels),
            config=ProjectConfig.model_validate(row.config) if row.config else None,
            lease_epoch=row.lease_epoch,
            active_run_id=UUID(row.active_run_id) if row.active_run_id else None,
            active_container_id=active.container_id if active else None,
        )

    def list_sessions(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> list[Session]:
        with self.transaction() as db:
            statement = select(SessionRow).order_by(SessionRow.created_at)
            if tenant_id is not None:
                statement = statement.where(SessionRow.tenant_id == tenant_id)
            if user_id is not None:
                statement = statement.where(SessionRow.user_id == user_id)
            if project_id is not None:
                statement = statement.where(SessionRow.project_id == str(project_id))
            rows = db.execute(statement).scalars()
            return [self.get_session(UUID(row.id), db=db) for row in rows]

    def save_session(self, session: Session) -> None:
        with self.transaction() as db:
            row = db.get(SessionRow, str(session.id))
            if not row:
                raise RepositoryConflict("session does not exist")
            row.lease_epoch = session.lease_epoch
            row.active_run_id = str(session.active_run_id) if session.active_run_id else None
            row.tenant_id = session.tenant_id
            row.user_id = session.user_id
            if session.project_id:
                row.project_id = str(session.project_id)
            row.labels = dict(session.metadata)
            row.config = (
                session.config.model_dump(mode="json") if session.config is not None else None
            )

    def create_runtime_operation(self, operation: str, resource_id: str) -> UUID:
        operation_id = uuid4()
        with self.transaction() as db:
            db.add(
                RuntimeOperationRow(
                    id=str(operation_id),
                    operation=operation,
                    resource_id=resource_id,
                    status="PENDING",
                    created_at=_now(),
                )
            )
        return operation_id

    def finish_runtime_operation(
        self, operation_id: UUID, status: str, result: dict[str, Any] | None = None
    ) -> None:
        with self.transaction() as db:
            row = db.get(RuntimeOperationRow, str(operation_id))
            if not row:
                raise RepositoryConflict("runtime operation does not exist")
            row.status = status
            row.result = result

    def create_conversation(
        self,
        conversation: Conversation,
        request_hash: str,
        idempotency_key: str | None,
    ) -> Conversation:
        with self.transaction() as db:
            self._lock_idempotency(db, "conversation", idempotency_key)
            existing = self._idempotent_resource(db, "conversation", idempotency_key, request_hash)
            if existing:
                return self.get_conversation(existing, db=db)
            self._insert_conversation(db, conversation)
            self._save_idempotency(
                db,
                "conversation",
                idempotency_key,
                request_hash,
                conversation.id,
                conversation.model_dump(mode="json"),
            )
            return conversation

    def delete_conversation(
        self, conversation_id: UUID, idempotency_key: str | None = None
    ) -> None:
        with self.transaction() as db:
            row = db.get(ConversationRow, str(conversation_id))
            if row:
                db.delete(row)
            if idempotency_key:
                db.execute(
                    delete(IdempotencyKeyRow).where(
                        IdempotencyKeyRow.scope == "conversation",
                        IdempotencyKeyRow.key == idempotency_key,
                    )
                )

    def _insert_conversation(self, db: DbSession, conversation: Conversation) -> None:
        db.add(
            ConversationRow(
                id=str(conversation.id),
                session_id=str(conversation.session_id),
                parent_conversation_id=(
                    str(conversation.parent_conversation_id)
                    if conversation.parent_conversation_id
                    else None
                ),
                task=conversation.task,
                execution_state=conversation.run.state.value,
                run_id=str(conversation.run.run_id),
                last_seq=conversation.run.last_seq,
                pending_interaction=conversation.run.pending_interaction,
                result_summary=conversation.run.result_summary,
                checkpoint_id=(
                    str(conversation.run.checkpoint_id) if conversation.run.checkpoint_id else None
                ),
                created_at=conversation.created_at,
            )
        )

    def get_conversation(
        self, conversation_id: UUID, *, db: DbSession | None = None
    ) -> Conversation | None:
        if db is None:
            with self.transaction() as tx:
                return self.get_conversation(conversation_id, db=tx)
        row = db.get(ConversationRow, str(conversation_id))
        if not row:
            return None
        return Conversation(
            id=UUID(row.id),
            session_id=UUID(row.session_id),
            parent_conversation_id=(
                UUID(row.parent_conversation_id) if row.parent_conversation_id else None
            ),
            task=row.task,
            created_at=row.created_at,
            run=RunProjection(
                run_id=UUID(row.run_id),
                state=ExecutionState(row.execution_state),
                last_seq=row.last_seq,
                pending_interaction=row.pending_interaction,
                result_summary=row.result_summary,
                checkpoint_id=UUID(row.checkpoint_id) if row.checkpoint_id else None,
            ),
        )

    def list_conversations(self, session_id: UUID | None = None) -> list[Conversation]:
        with self.transaction() as db:
            statement = select(ConversationRow).order_by(ConversationRow.created_at)
            if session_id is not None:
                statement = statement.where(
                    ConversationRow.session_id == str(session_id)
                )
            rows = db.execute(statement).scalars()
            return [self.get_conversation(UUID(row.id), db=db) for row in rows]

    def append_event(self, conversation: Conversation, event: EventEnvelope) -> None:
        with self.transaction() as db:
            row = db.execute(
                select(ConversationRow)
                .where(ConversationRow.id == str(conversation.id))
                .with_for_update()
            ).scalar_one_or_none()
            if not row:
                raise RepositoryConflict("conversation does not exist")
            expected = row.last_seq + 1
            if event.seq != expected:
                raise RepositoryConflict(
                    f"event sequence conflict: expected {expected}, got {event.seq}"
                )
            db.add(
                ConversationEventRow(
                    id=str(event.event_id),
                    conversation_id=str(conversation.id),
                    run_id=str(event.run_id),
                    seq=event.seq,
                    type=event.type,
                    payload=event.payload,
                    source=event.source,
                    occurred_at=event.occurred_at,
                )
            )
            db.add(
                OutboxEventRow(
                    topic="conversation.events",
                    aggregate_id=str(conversation.id),
                    payload=event.model_dump(mode="json"),
                    created_at=_now(),
                )
            )
            row.execution_state = conversation.run.state.value
            row.last_seq = event.seq
            row.pending_interaction = conversation.run.pending_interaction
            row.result_summary = conversation.run.result_summary
            row.checkpoint_id = (
                str(conversation.run.checkpoint_id) if conversation.run.checkpoint_id else None
            )

    def get_checkpoint(
        self, checkpoint_id: UUID, *, db: DbSession | None = None
    ) -> Checkpoint | None:
        if db is None:
            with self.transaction() as tx:
                return self.get_checkpoint(checkpoint_id, db=tx)
        row = db.get(ConversationCheckpointRow, str(checkpoint_id))
        if not row:
            return None
        context = ContextBundle.model_validate(row.context_bundle)
        pending_interaction = row.pending_interaction
        tool_policy = context.tool_policy
        pending_tool_calls = list(tool_policy.get("pending_tool_calls", []))
        tool_batch_hash = tool_policy.get("tool_batch_hash")
        if not pending_tool_calls and isinstance(pending_interaction, dict):
            raw_batch = pending_interaction.get("tool_batch")
            if isinstance(raw_batch, dict):
                pending_tool_calls = list(raw_batch.get("calls", []))
        if not tool_batch_hash and isinstance(pending_interaction, dict):
            tool_batch_hash = pending_interaction.get("tool_batch_hash")
        return Checkpoint(
            checkpoint_id=UUID(row.id),
            conversation_id=UUID(row.conversation_id),
            run_id=UUID(row.run_id),
            last_event_seq=row.last_event_seq,
            context_bundle=context,
            context_bundle_hash=row.context_bundle_hash,
            pending_interaction=pending_interaction,
            pending_tool_calls=pending_tool_calls,
            tool_batch_hash=tool_batch_hash,
            tool_policy_hash=(row.tool_policy_hash or tool_policy.get("tool_policy_hash")),
            tool_versions_hash=(row.tool_versions_hash or tool_policy.get("tool_versions_hash")),
            workspace_ref=row.context_bundle.get("workspace_ref", "/workspace"),
            workspace_write_lease_epoch=row.workspace_write_lease_epoch,
            core_type=row.core_type or "fake",
            core_version=row.core_version or "0.1.0",
            created_at=row.created_at,
        )

    def list_events(self, conversation_id: UUID, after_seq: int = 0) -> list[EventEnvelope]:
        with self.transaction() as db:
            rows = db.execute(
                select(ConversationEventRow)
                .where(
                    ConversationEventRow.conversation_id == str(conversation_id),
                    ConversationEventRow.seq > after_seq,
                )
                .order_by(ConversationEventRow.seq)
            ).scalars()
            return [
                EventEnvelope(
                    event_id=UUID(row.id),
                    run_id=UUID(row.run_id),
                    seq=row.seq,
                    type=row.type,
                    payload=row.payload,
                    source=row.source,
                    occurred_at=row.occurred_at,
                )
                for row in rows
            ]

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        with self.transaction() as db:
            db.add(self._checkpoint_row(checkpoint))

    @staticmethod
    def _checkpoint_row(checkpoint: Checkpoint) -> ConversationCheckpointRow:
        return ConversationCheckpointRow(
            id=str(checkpoint.checkpoint_id),
            conversation_id=str(checkpoint.conversation_id),
            run_id=str(checkpoint.run_id),
            last_event_seq=checkpoint.last_event_seq,
            context_bundle=checkpoint.context_bundle.model_dump(mode="json"),
            context_bundle_hash=checkpoint.context_bundle_hash,
            pending_interaction=checkpoint.pending_interaction,
            core_type=checkpoint.core_type,
            core_version=checkpoint.core_version,
            tool_policy_hash=checkpoint.tool_policy_hash,
            tool_versions_hash=checkpoint.tool_versions_hash,
            workspace_write_lease_epoch=checkpoint.workspace_write_lease_epoch,
            created_at=checkpoint.created_at,
        )

    def save_checkpoint_and_append_event(
        self, checkpoint: Checkpoint, conversation: Conversation, event: EventEnvelope
    ) -> None:
        with self.transaction() as db:
            row = db.execute(
                select(ConversationRow)
                .where(ConversationRow.id == str(conversation.id))
                .with_for_update()
            ).scalar_one_or_none()
            if not row:
                raise RepositoryConflict("conversation does not exist")
            expected = row.last_seq + 1
            if event.seq != expected:
                raise RepositoryConflict(
                    f"event sequence conflict: expected {expected}, got {event.seq}"
                )
            db.add(self._checkpoint_row(checkpoint))
            db.add(
                ConversationEventRow(
                    id=str(event.event_id),
                    conversation_id=str(conversation.id),
                    run_id=str(event.run_id),
                    seq=event.seq,
                    type=event.type,
                    payload=event.payload,
                    source=event.source,
                    occurred_at=event.occurred_at,
                )
            )
            db.add(
                OutboxEventRow(
                    topic="conversation.events",
                    aggregate_id=str(conversation.id),
                    payload=event.model_dump(mode="json"),
                    created_at=_now(),
                )
            )
            row.execution_state = conversation.run.state.value
            row.last_seq = event.seq
            row.pending_interaction = conversation.run.pending_interaction
            row.result_summary = conversation.run.result_summary
            row.checkpoint_id = str(conversation.run.checkpoint_id)

    def active_container_count(self) -> int:
        with self.transaction() as db:
            return int(
                db.execute(
                    select(func.count())
                    .select_from(ContainerLeaseRow)
                    .where(ContainerLeaseRow.status == "ACTIVE")
                ).scalar_one()
            )

    def try_acquire_workspace_lease(
        self,
        workspace_id: UUID,
        session_id: UUID,
        lease_epoch: int,
        container_id: str,
        runtime_operation_id: UUID | None = None,
    ) -> bool:
        with self.transaction() as db:
            row = db.get(WorkspaceWriteLeaseRow, str(workspace_id), with_for_update=True)
            if row and row.status == "ACTIVE" and row.session_id != str(session_id):
                return False
            if row:
                row.session_id = str(session_id)
                row.lease_epoch = lease_epoch
                row.status = "ACTIVE"
            else:
                db.add(
                    WorkspaceWriteLeaseRow(
                        workspace_id=str(workspace_id),
                        session_id=str(session_id),
                        lease_epoch=lease_epoch,
                        status="ACTIVE",
                    )
                )
            container = db.get(ContainerLeaseRow, str(session_id), with_for_update=True)
            if container:
                container.container_id = container_id
                container.lease_epoch = lease_epoch
                container.status = "ACTIVE"
                container.runtime_operation_id = (
                    str(runtime_operation_id) if runtime_operation_id else None
                )
            else:
                db.add(
                    ContainerLeaseRow(
                        session_id=str(session_id),
                        container_id=container_id,
                        lease_epoch=lease_epoch,
                        status="ACTIVE",
                        runtime_operation_id=(
                            str(runtime_operation_id) if runtime_operation_id else None
                        ),
                    )
                )
            return True

    def release_session_leases(self, session: Session) -> None:
        with self.transaction() as db:
            container = db.execute(
                select(ContainerLeaseRow).where(ContainerLeaseRow.session_id == str(session.id))
            ).scalar_one_or_none()
            if container:
                container.status = "STOPPED"
            workspace = db.get(WorkspaceWriteLeaseRow, str(session.workspace_id))
            if workspace and workspace.session_id == str(session.id):
                workspace.status = "RELEASED"

    @staticmethod
    def _job(row: ExecutionJobRow) -> ExecutionJob:
        return ExecutionJob(
            id=UUID(row.id),
            run_id=UUID(row.run_id),
            conversation_id=UUID(row.conversation_id),
            session_id=UUID(row.session_id),
            workspace_id=UUID(row.workspace_id),
            state=JobState(row.state),
            priority=row.priority,
            available_at=_as_utc(row.available_at),
            claimed_by=row.claimed_by,
            claim_token=row.claim_token,
            lease_expires_at=_as_utc(row.lease_expires_at),
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            last_error=row.last_error,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            state_changed_at=_as_utc(row.state_changed_at),
        )

    def create_conversation_and_enqueue(
        self,
        conversation: Conversation,
        workspace_id: UUID,
        request_hash: str,
        idempotency_key: str | None,
        *,
        max_attempts: int = 3,
    ) -> tuple[Conversation, bool]:
        """Create the projection, initial event, job, and outbox record atomically."""

        with self.transaction() as db:
            self._lock_idempotency(db, "conversation", idempotency_key)
            existing = self._idempotent_resource(db, "conversation", idempotency_key, request_hash)
            if existing:
                persisted = self.get_conversation(existing, db=db)
                if persisted is None:
                    raise RepositoryConflict("idempotent conversation does not exist")
                return persisted, False
            conversation.run.state = ExecutionState.QUEUED
            conversation.run.last_seq = 1
            event = EventEnvelope(
                run_id=conversation.run.run_id,
                seq=1,
                type="conversation.queued",
                payload={"session_id": str(conversation.session_id)},
            )
            self._insert_conversation(db, conversation)
            now = _now()
            db.add(
                ConversationEventRow(
                    id=str(event.event_id),
                    conversation_id=str(conversation.id),
                    run_id=str(event.run_id),
                    seq=event.seq,
                    type=event.type,
                    payload=event.payload,
                    source=event.source,
                    occurred_at=event.occurred_at,
                )
            )
            db.add(
                ExecutionJobRow(
                    run_id=str(conversation.run.run_id),
                    conversation_id=str(conversation.id),
                    session_id=str(conversation.session_id),
                    workspace_id=str(workspace_id),
                    state=JobState.READY.value,
                    priority=0,
                    available_at=now,
                    claim_token=0,
                    attempts=0,
                    max_attempts=max_attempts,
                    created_at=now,
                    updated_at=now,
                    state_changed_at=now,
                )
            )
            db.add(
                OutboxEventRow(
                    topic="conversation.events",
                    aggregate_id=str(conversation.id),
                    payload=event.model_dump(mode="json"),
                    created_at=now,
                )
            )
            self._save_idempotency(
                db,
                "conversation",
                idempotency_key,
                request_hash,
                conversation.id,
                conversation.model_dump(mode="json"),
            )
            return conversation, True

    def get_execution_job(self, run_id: UUID) -> ExecutionJob | None:
        with self.transaction() as db:
            row = db.execute(
                select(ExecutionJobRow).where(ExecutionJobRow.run_id == str(run_id))
            ).scalar_one_or_none()
            return self._job(row) if row else None

    def ensure_runtime_slots(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("runtime capacity must be at least one")
        values = [{"slot_no": slot_no} for slot_no in range(1, capacity + 1)]
        with self.transaction() as db:
            if self.engine.dialect.name == "postgresql":
                db.execute(postgres_insert(RuntimeSlotRow).values(values).on_conflict_do_nothing())
            elif self.engine.dialect.name == "sqlite":
                db.execute(sqlite_insert(RuntimeSlotRow).values(values).on_conflict_do_nothing())
            else:
                for value in values:
                    if db.get(RuntimeSlotRow, value["slot_no"]) is None:
                        db.add(RuntimeSlotRow(**value))

    def claim_next_job(
        self,
        worker_id: str,
        *,
        capacity: int,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> JobClaim | None:
        self.ensure_runtime_slots(capacity)
        claimed_at = now or _now()
        lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        with self.transaction() as db:
            slot = db.execute(
                select(RuntimeSlotRow)
                .where(
                    or_(
                        RuntimeSlotRow.run_id.is_(None),
                        RuntimeSlotRow.lease_expires_at < claimed_at,
                    )
                )
                .order_by(RuntimeSlotRow.slot_no)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if slot is None:
                return None
            claimable = or_(
                and_(
                    ExecutionJobRow.state.in_([JobState.READY.value, JobState.RETRY.value]),
                    ExecutionJobRow.available_at <= claimed_at,
                ),
                and_(
                    ExecutionJobRow.state.in_(
                        [
                            JobState.CLAIMED.value,
                            JobState.RUNNING.value,
                            JobState.WAITING.value,
                        ]
                    ),
                    ExecutionJobRow.lease_expires_at < claimed_at,
                ),
            )
            candidate_ids = list(
                db.execute(
                    select(ExecutionJobRow.id)
                    .where(claimable)
                    .order_by(ExecutionJobRow.priority.desc(), ExecutionJobRow.created_at)
                    .limit(100)
                ).scalars()
            )
            for candidate_id in candidate_ids:
                row = db.execute(
                    select(ExecutionJobRow)
                    .where(ExecutionJobRow.id == candidate_id, claimable)
                    .with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if row is None:
                    continue
                previous_state = JobState(row.state)
                # Locking the Workspace row serializes creation/replacement of its lease row.
                workspace = db.execute(
                    select(WorkspaceRow)
                    .where(WorkspaceRow.id == row.workspace_id)
                    .with_for_update()
                ).scalar_one()
                del workspace
                write_lease = db.get(
                    WorkspaceWriteLeaseRow, row.workspace_id, with_for_update=True
                )
                if (
                    write_lease
                    and write_lease.status == "ACTIVE"
                    and write_lease.run_id != row.run_id
                    and write_lease.expires_at is not None
                    and _as_utc(write_lease.expires_at) >= claimed_at
                ):
                    continue
                session = db.get(SessionRow, row.session_id, with_for_update=True)
                if session is None:
                    row.state = JobState.FAILED.value
                    row.state_changed_at = claimed_at
                    row.last_error = {"code": "SESSION_NOT_FOUND"}
                    row.updated_at = claimed_at
                    continue
                endpoint = db.get(RunnerEndpointRow, row.run_id, with_for_update=True)
                if (
                    previous_state not in {JobState.RUNNING, JobState.WAITING}
                    or endpoint is None
                    or endpoint.status != "READY"
                ):
                    session.lease_epoch += 1
                session.active_run_id = row.run_id
                row.claim_token += 1
                row.claimed_by = worker_id
                row.lease_expires_at = lease_expires_at
                row.state = JobState.CLAIMED.value
                row.state_changed_at = claimed_at
                row.attempts += 1
                row.updated_at = claimed_at
                slot.run_id = row.run_id
                slot.claim_token = row.claim_token
                slot.lease_expires_at = lease_expires_at
                if write_lease is None:
                    write_lease = WorkspaceWriteLeaseRow(
                        workspace_id=row.workspace_id,
                        session_id=row.session_id,
                        lease_epoch=session.lease_epoch,
                        status="ACTIVE",
                    )
                    db.add(write_lease)
                write_lease.session_id = row.session_id
                write_lease.run_id = row.run_id
                write_lease.lease_epoch = session.lease_epoch
                write_lease.status = "ACTIVE"
                write_lease.owner_instance_id = worker_id
                write_lease.heartbeat_at = claimed_at
                write_lease.expires_at = lease_expires_at
                if endpoint is not None:
                    endpoint.owner_instance_id = worker_id
                    endpoint.lease_epoch = session.lease_epoch
                    endpoint.heartbeat_at = claimed_at
                db.flush()
                return JobClaim(
                    job=self._job(row),
                    previous_state=previous_state,
                    claim_token=row.claim_token,
                    lease_expires_at=lease_expires_at,
                    slot_no=slot.slot_no,
                    fence_epoch=session.lease_epoch,
                )
            return None

    def adopt_runner(self, claim: JobClaim, worker_id: str) -> RunnerEndpoint:
        if claim.previous_state not in {JobState.RUNNING, JobState.WAITING}:
            raise ValueError("claim does not represent a running Runner")
        now = _now()
        with self.transaction() as db:
            job = self._claimed_job(db, claim.job.run_id, worker_id, claim.claim_token)
            endpoint = db.get(RunnerEndpointRow, str(claim.job.run_id), with_for_update=True)
            if endpoint is None or endpoint.status != "READY":
                raise RepositoryConflict("Runner endpoint is unavailable")
            job.state = claim.previous_state.value
            job.updated_at = now
            endpoint.owner_instance_id = worker_id
            endpoint.heartbeat_at = now
            return RunnerEndpoint(
                run_id=UUID(endpoint.run_id),
                session_id=UUID(endpoint.session_id),
                runtime_id=endpoint.runtime_id,
                endpoint=endpoint.endpoint,
                lease_epoch=endpoint.lease_epoch,
                owner_instance_id=endpoint.owner_instance_id,
                status=endpoint.status,
                last_runner_seq=endpoint.last_runner_seq,
                heartbeat_at=now,
            )

    @staticmethod
    def _claimed_job(
        db: DbSession, run_id: UUID, worker_id: str, claim_token: int
    ) -> ExecutionJobRow:
        row = db.execute(
            select(ExecutionJobRow)
            .where(ExecutionJobRow.run_id == str(run_id))
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise RepositoryConflict("execution job does not exist")
        if row.claimed_by != worker_id or row.claim_token != claim_token:
            raise StaleClaim("execution job claim is stale")
        return row

    def renew_job_claim(
        self,
        run_id: UUID,
        worker_id: str,
        claim_token: int,
        *,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> datetime:
        heartbeat_at = now or _now()
        expires_at = heartbeat_at + timedelta(seconds=lease_seconds)
        with self.transaction() as db:
            row = self._claimed_job(db, run_id, worker_id, claim_token)
            if row.state not in {JobState.CLAIMED.value, JobState.RUNNING.value, JobState.WAITING.value}:
                raise StaleClaim("execution job is no longer renewable")
            row.lease_expires_at = expires_at
            row.updated_at = heartbeat_at
            slot = db.execute(
                select(RuntimeSlotRow)
                .where(
                    RuntimeSlotRow.run_id == str(run_id),
                    RuntimeSlotRow.claim_token == claim_token,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if slot is None:
                raise StaleClaim("runtime slot is no longer owned by this claim")
            slot.lease_expires_at = expires_at
            lease = db.execute(
                select(WorkspaceWriteLeaseRow)
                .where(
                    WorkspaceWriteLeaseRow.run_id == str(run_id),
                    WorkspaceWriteLeaseRow.owner_instance_id == worker_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if lease is None:
                raise StaleClaim("workspace lease is no longer owned by this claim")
            lease.heartbeat_at = heartbeat_at
            lease.expires_at = expires_at
            endpoint = db.get(RunnerEndpointRow, str(run_id), with_for_update=True)
            if endpoint:
                endpoint.heartbeat_at = heartbeat_at
            return expires_at

    def register_runner(
        self,
        claim: JobClaim,
        worker_id: str,
        runtime_id: str,
        endpoint: str,
    ) -> RunnerEndpoint:
        now = _now()
        with self.transaction() as db:
            row = self._claimed_job(db, claim.job.run_id, worker_id, claim.claim_token)
            if row.state != JobState.CLAIMED.value:
                raise StaleClaim("execution job has already left the claimed state")
            row.state = JobState.RUNNING.value
            row.updated_at = now
            row.state_changed_at = now
            endpoint_row = RunnerEndpointRow(
                run_id=row.run_id,
                session_id=row.session_id,
                runtime_id=runtime_id,
                endpoint=endpoint,
                lease_epoch=claim.fence_epoch,
                owner_instance_id=worker_id,
                status="READY",
                last_runner_seq=0,
                heartbeat_at=now,
            )
            db.merge(endpoint_row)
            container = db.get(ContainerLeaseRow, row.session_id, with_for_update=True)
            if container is None:
                container = ContainerLeaseRow(
                    session_id=row.session_id,
                    container_id=runtime_id,
                    lease_epoch=claim.fence_epoch,
                    status="ACTIVE",
                )
                db.add(container)
            container.container_id = runtime_id
            container.run_id = row.run_id
            container.lease_epoch = claim.fence_epoch
            container.status = "ACTIVE"
            container.owner_instance_id = worker_id
            container.heartbeat_at = now
            container.expires_at = row.lease_expires_at
            return RunnerEndpoint(
                run_id=UUID(row.run_id),
                session_id=UUID(row.session_id),
                runtime_id=runtime_id,
                endpoint=endpoint,
                lease_epoch=claim.fence_epoch,
                owner_instance_id=worker_id,
                status="READY",
                heartbeat_at=now,
            )

    def get_runner_endpoint(self, run_id: UUID) -> RunnerEndpoint | None:
        with self.transaction() as db:
            row = db.get(RunnerEndpointRow, str(run_id))
            if row is None:
                return None
            return RunnerEndpoint(
                run_id=UUID(row.run_id),
                session_id=UUID(row.session_id),
                runtime_id=row.runtime_id,
                endpoint=row.endpoint,
                lease_epoch=row.lease_epoch,
                owner_instance_id=row.owner_instance_id,
                status=row.status,
                last_runner_seq=row.last_runner_seq,
                heartbeat_at=_as_utc(row.heartbeat_at),
            )

    def list_active_runner_endpoints(self) -> list[RunnerEndpoint]:
        with self.transaction() as db:
            rows = db.execute(
                select(RunnerEndpointRow).where(RunnerEndpointRow.status == "READY")
            ).scalars()
            return [
                RunnerEndpoint(
                    run_id=UUID(row.run_id),
                    session_id=UUID(row.session_id),
                    runtime_id=row.runtime_id,
                    endpoint=row.endpoint,
                    lease_epoch=row.lease_epoch,
                    owner_instance_id=row.owner_instance_id,
                    status=row.status,
                    last_runner_seq=row.last_runner_seq,
                    heartbeat_at=_as_utc(row.heartbeat_at),
                )
                for row in rows
            ]

    def mark_runtime_missing(self, run_id: UUID) -> None:
        expired_at = _now() - timedelta(seconds=1)
        with self.transaction() as db:
            endpoint = db.get(RunnerEndpointRow, str(run_id), with_for_update=True)
            if endpoint is None:
                return
            endpoint.status = "MISSING"
            job = db.execute(
                select(ExecutionJobRow)
                .where(ExecutionJobRow.run_id == str(run_id))
                .with_for_update()
            ).scalar_one_or_none()
            if job and job.state in {JobState.RUNNING.value, JobState.WAITING.value}:
                job.lease_expires_at = expired_at
                job.updated_at = _now()
            slot = db.execute(
                select(RuntimeSlotRow)
                .where(RuntimeSlotRow.run_id == str(run_id))
                .with_for_update()
            ).scalar_one_or_none()
            if slot:
                slot.lease_expires_at = expired_at
            lease = db.execute(
                select(WorkspaceWriteLeaseRow)
                .where(WorkspaceWriteLeaseRow.run_id == str(run_id))
                .with_for_update()
            ).scalar_one_or_none()
            if lease:
                lease.expires_at = expired_at

    def save_runner_registration(
        self, registration: RunnerRegistration, token_hash: str
    ) -> None:
        with self.transaction() as db:
            existing = db.get(RunnerRegistrationRow, str(registration.runner_id))
            if existing is not None:
                raise RepositoryConflict("runner is already registered")
            db.add(
                RunnerRegistrationRow(
                    runner_id=str(registration.runner_id),
                    provider=registration.provider,
                    endpoint=registration.endpoint,
                    version=registration.version,
                    capabilities=list(registration.capabilities),
                    status=registration.status,
                    load=registration.load,
                    token_hash=token_hash,
                    labels=dict(registration.metadata),
                    last_heartbeat_at=registration.last_heartbeat_at,
                    created_at=registration.created_at,
                )
            )

    def get_runner_registration(self, runner_id: UUID) -> RunnerRegistration | None:
        with self.transaction() as db:
            row = db.get(RunnerRegistrationRow, str(runner_id))
            if row is None:
                return None
            return self._runner_registration_from_row(row)

    def get_runner_registration_hash(self, runner_id: UUID) -> str | None:
        with self.transaction() as db:
            row = db.get(RunnerRegistrationRow, str(runner_id))
            return row.token_hash if row is not None else None

    def update_runner_registration(
        self,
        runner_id: UUID,
        *,
        status: str,
        load: int,
        capabilities: list[str] | None,
        heartbeat_at,
    ) -> RunnerRegistration | None:
        with self.transaction() as db:
            row = db.get(RunnerRegistrationRow, str(runner_id))
            if row is None:
                return None
            row.status = status
            row.load = load
            if capabilities is not None:
                row.capabilities = list(capabilities)
            row.last_heartbeat_at = heartbeat_at
            return self._runner_registration_from_row(row)

    def delete_runner_registration(self, runner_id: UUID) -> bool:
        with self.transaction() as db:
            row = db.get(RunnerRegistrationRow, str(runner_id))
            if row is None:
                return False
            db.delete(row)
            return True

    def list_ready_runner_registrations(self) -> list[RunnerRegistration]:
        with self.transaction() as db:
            rows = db.execute(
                select(RunnerRegistrationRow).where(
                    RunnerRegistrationRow.status == "READY"
                )
            ).scalars()
            return [self._runner_registration_from_row(row) for row in rows]

    def expire_runner_registrations(
        self, *, now, timeout_seconds: float
    ) -> list[str]:
        cutoff = now - timedelta(seconds=timeout_seconds)
        with self.transaction() as db:
            stale = (
                db.execute(
                    select(RunnerRegistrationRow.runner_id).where(
                        RunnerRegistrationRow.last_heartbeat_at < cutoff
                    )
                )
                .scalars()
                .all()
            )
            if stale:
                db.execute(
                    delete(RunnerRegistrationRow).where(
                        RunnerRegistrationRow.runner_id.in_(list(stale))
                    )
                )
            return list(stale)

    @staticmethod
    def _runner_registration_from_row(row: RunnerRegistrationRow) -> RunnerRegistration:
        return RunnerRegistration(
            runner_id=UUID(row.runner_id),
            provider=row.provider,
            endpoint=row.endpoint,
            version=row.version,
            capabilities=list(row.capabilities),
            status=row.status,
            load=row.load,
            last_heartbeat_at=_as_utc(row.last_heartbeat_at),
            created_at=_as_utc(row.created_at),
            metadata=dict(row.labels),
        )

    def pause_expired_waiting(
        self,
        waiting_seconds: int,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[RunnerEndpoint]:
        paused_at = now or _now()
        cutoff = paused_at - timedelta(seconds=waiting_seconds)
        with self.transaction() as db:
            jobs = list(
                db.execute(
                    select(ExecutionJobRow)
                    .where(
                        ExecutionJobRow.state == JobState.WAITING.value,
                        ExecutionJobRow.state_changed_at <= cutoff,
                    )
                    .order_by(ExecutionJobRow.state_changed_at)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                ).scalars()
            )
            endpoints: list[RunnerEndpoint] = []
            for job in jobs:
                conversation = db.execute(
                    select(ConversationRow)
                    .where(ConversationRow.run_id == job.run_id)
                    .with_for_update()
                ).scalar_one()
                if conversation.checkpoint_id is None:
                    continue
                endpoint = db.get(RunnerEndpointRow, job.run_id, with_for_update=True)
                if endpoint:
                    endpoints.append(
                        RunnerEndpoint(
                            run_id=UUID(endpoint.run_id),
                            session_id=UUID(endpoint.session_id),
                            runtime_id=endpoint.runtime_id,
                            endpoint=endpoint.endpoint,
                            lease_epoch=endpoint.lease_epoch,
                            owner_instance_id=endpoint.owner_instance_id,
                            status="STOPPING",
                            last_runner_seq=endpoint.last_runner_seq,
                            heartbeat_at=_as_utc(endpoint.heartbeat_at),
                        )
                    )
                    endpoint.status = "STOPPING"
                job.state = JobState.PAUSED.value
                job.state_changed_at = paused_at
                job.updated_at = paused_at
                job.claimed_by = None
                job.lease_expires_at = None
                conversation.execution_state = ExecutionState.PAUSED.value
                seq = conversation.last_seq + 1
                event = EventEnvelope(
                    run_id=UUID(job.run_id),
                    seq=seq,
                    type="run.paused",
                    payload={"reason": "waiting_input_timeout"},
                    source="agentsupport_reconciler",
                )
                db.add(
                    ConversationEventRow(
                        id=str(event.event_id),
                        conversation_id=conversation.id,
                        run_id=job.run_id,
                        seq=seq,
                        type=event.type,
                        payload=event.payload,
                        source=event.source,
                        occurred_at=event.occurred_at,
                    )
                )
                db.add(
                    OutboxEventRow(
                        topic="conversation.events",
                        aggregate_id=conversation.id,
                        payload=event.model_dump(mode="json"),
                        created_at=paused_at,
                    )
                )
                conversation.last_seq = seq
                slot = db.execute(
                    select(RuntimeSlotRow)
                    .where(RuntimeSlotRow.run_id == job.run_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if slot:
                    slot.run_id = None
                    slot.claim_token = None
                    slot.lease_expires_at = None
                lease = db.execute(
                    select(WorkspaceWriteLeaseRow)
                    .where(WorkspaceWriteLeaseRow.run_id == job.run_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if lease:
                    lease.status = "RELEASED"
                    lease.expires_at = None
                container = db.execute(
                    select(ContainerLeaseRow)
                    .where(ContainerLeaseRow.run_id == job.run_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if container:
                    container.status = "STOPPED"
                    container.expires_at = None
                session = db.get(SessionRow, job.session_id, with_for_update=True)
                if session and session.active_run_id == job.run_id:
                    session.active_run_id = None
            return endpoints

    def finish_execution_job(
        self,
        run_id: UUID,
        worker_id: str,
        claim_token: int,
        state: JobState,
        *,
        error: dict[str, Any] | None = None,
        retry_delay_seconds: int = 5,
    ) -> ExecutionJob:
        if state not in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.RETRY}:
            raise ValueError("invalid finishing state")
        now = _now()
        with self.transaction() as db:
            row = self._claimed_job(db, run_id, worker_id, claim_token)
            if state == JobState.RETRY and row.attempts >= row.max_attempts:
                state = JobState.FAILED
            row.state = state.value
            row.state_changed_at = now
            row.last_error = error
            row.updated_at = now
            row.available_at = now + timedelta(seconds=retry_delay_seconds)
            row.lease_expires_at = None
            row.claimed_by = None
            slot = db.execute(
                select(RuntimeSlotRow)
                .where(
                    RuntimeSlotRow.run_id == str(run_id),
                    RuntimeSlotRow.claim_token == claim_token,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if slot:
                slot.run_id = None
                slot.claim_token = None
                slot.lease_expires_at = None
            endpoint = db.get(RunnerEndpointRow, str(run_id), with_for_update=True)
            if endpoint:
                endpoint.status = "STOPPED"
            lease = db.execute(
                select(WorkspaceWriteLeaseRow)
                .where(WorkspaceWriteLeaseRow.run_id == str(run_id))
                .with_for_update()
            ).scalar_one_or_none()
            if lease:
                lease.status = "RELEASED"
                lease.expires_at = None
            container = db.execute(
                select(ContainerLeaseRow)
                .where(ContainerLeaseRow.run_id == str(run_id))
                .with_for_update()
            ).scalar_one_or_none()
            if container:
                container.status = "STOPPED"
                container.expires_at = None
            session = db.get(SessionRow, row.session_id, with_for_update=True)
            if session and session.active_run_id == row.run_id:
                session.active_run_id = None
            db.flush()
            return self._job(row)

    def queue_depth(self) -> int:
        with self.transaction() as db:
            return int(
                db.execute(
                    select(func.count())
                    .select_from(ExecutionJobRow)
                    .where(ExecutionJobRow.state.in_([JobState.READY.value, JobState.RETRY.value]))
                ).scalar_one()
            )

    def health_check(self) -> None:
        with self.transaction() as db:
            db.execute(text("SELECT 1"))

    def coordination_metrics(self) -> dict[str, int]:
        now = _now()
        with self.transaction() as db:
            states = dict(
                db.execute(
                    select(ExecutionJobRow.state, func.count()).group_by(ExecutionJobRow.state)
                ).all()
            )
            oldest_ready = db.execute(
                select(func.min(ExecutionJobRow.state_changed_at)).where(
                    ExecutionJobRow.state.in_([JobState.READY.value, JobState.RETRY.value]),
                    ExecutionJobRow.available_at <= now,
                )
            ).scalar_one()
            expired_claims = int(
                db.execute(
                    select(func.count())
                    .select_from(ExecutionJobRow)
                    .where(
                        ExecutionJobRow.state.in_(
                            [
                                JobState.CLAIMED.value,
                                JobState.RUNNING.value,
                                JobState.WAITING.value,
                            ]
                        ),
                        ExecutionJobRow.lease_expires_at < now,
                    )
                ).scalar_one()
            )
            active_runtimes = int(
                db.execute(
                    select(func.count())
                    .select_from(RunnerEndpointRow)
                    .where(RunnerEndpointRow.status == "READY")
                ).scalar_one()
            )
            outbox_pending = int(
                db.execute(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(OutboxEventRow.published_at.is_(None))
                ).scalar_one()
            )
            oldest_outbox = db.execute(
                select(func.min(OutboxEventRow.created_at)).where(
                    OutboxEventRow.published_at.is_(None)
                )
            ).scalar_one()
            workspace_lease_contention = int(
                db.execute(
                    select(func.count())
                    .select_from(ExecutionJobRow)
                    .join(
                        WorkspaceWriteLeaseRow,
                        WorkspaceWriteLeaseRow.workspace_id == ExecutionJobRow.workspace_id,
                    )
                    .where(
                        ExecutionJobRow.state.in_(
                            [JobState.READY.value, JobState.RETRY.value]
                        ),
                        WorkspaceWriteLeaseRow.status == "ACTIVE",
                        WorkspaceWriteLeaseRow.run_id != ExecutionJobRow.run_id,
                        WorkspaceWriteLeaseRow.expires_at >= now,
                    )
                ).scalar_one()
            )
            runner_reconciliation_needed = int(
                db.execute(
                    select(func.count())
                    .select_from(RunnerEndpointRow)
                    .join(
                        ExecutionJobRow,
                        ExecutionJobRow.run_id == RunnerEndpointRow.run_id,
                    )
                    .where(
                        RunnerEndpointRow.status == "READY",
                        ExecutionJobRow.lease_expires_at < now,
                    )
                ).scalar_one()
            )

            def age_seconds(value: datetime | None) -> int:
                timestamp = _as_utc(value)
                return max(0, int((now - timestamp).total_seconds())) if timestamp else 0

            return {
                "queue_ready": int(states.get(JobState.READY.value, 0))
                + int(states.get(JobState.RETRY.value, 0)),
                "queue_oldest_ready_seconds": age_seconds(oldest_ready),
                "jobs_claimed": int(states.get(JobState.CLAIMED.value, 0)),
                "jobs_running": int(states.get(JobState.RUNNING.value, 0)),
                "jobs_waiting": int(states.get(JobState.WAITING.value, 0)),
                "jobs_paused": int(states.get(JobState.PAUSED.value, 0)),
                "claims_expired": expired_claims,
                "active_runtimes": active_runtimes,
                "runner_starting": int(states.get(JobState.CLAIMED.value, 0)),
                "runner_reconciliation_needed": runner_reconciliation_needed,
                "workspace_lease_contention": workspace_lease_contention,
                "outbox_pending": outbox_pending,
                "outbox_publication_lag_seconds": age_seconds(oldest_outbox),
            }

    def enqueue_command(
        self,
        run_id: UUID,
        command_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> RunCommand:
        with self.transaction() as db:
            existing = db.execute(
                select(RunCommandRow).where(
                    RunCommandRow.run_id == str(run_id),
                    RunCommandRow.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if existing:
                if existing.type != command_type or existing.payload != payload:
                    raise RepositoryConflict("command idempotency key was reused")
                row = existing
            else:
                row = RunCommandRow(
                    run_id=str(run_id),
                    type=command_type,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    state=CommandState.PENDING.value,
                    attempts=0,
                    created_at=_now(),
                )
                db.add(row)
                db.flush()
            return RunCommand(
                id=UUID(row.id),
                run_id=UUID(row.run_id),
                type=row.type,
                payload=row.payload,
                idempotency_key=row.idempotency_key,
                state=CommandState(row.state),
                claimed_by=row.claimed_by,
                attempts=row.attempts,
                created_at=_as_utc(row.created_at),
            )

    def enqueue_conversation_command(
        self,
        conversation_id: UUID,
        command_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        expected_seq: int | None = None,
        interaction_id: str | None = None,
    ) -> tuple[RunCommand, Conversation]:
        with self.transaction() as db:
            conversation_row = db.execute(
                select(ConversationRow)
                .where(ConversationRow.id == str(conversation_id))
                .with_for_update()
            ).scalar_one_or_none()
            if conversation_row is None:
                raise RepositoryConflict("conversation does not exist")
            existing = db.execute(
                select(RunCommandRow).where(
                    RunCommandRow.run_id == conversation_row.run_id,
                    RunCommandRow.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if existing:
                if existing.type != command_type or existing.payload != payload:
                    raise RepositoryConflict("command idempotency key was reused")
                command_row = existing
                created = False
            else:
                if expected_seq is not None and conversation_row.last_seq != expected_seq:
                    raise RepositoryConflict("expected_seq does not match conversation")
                if conversation_row.execution_state in {
                    ExecutionState.COMPLETED.value,
                    ExecutionState.FAILED.value,
                    ExecutionState.CANCELLED.value,
                    ExecutionState.LOST.value,
                }:
                    if command_type == "cancel":
                        conversation = self.get_conversation(conversation_id, db=db)
                        command = RunCommand(
                            id=uuid4(),
                            run_id=UUID(conversation_row.run_id),
                            type=command_type,
                            payload=payload,
                            idempotency_key=idempotency_key,
                            state=CommandState.APPLIED,
                            created_at=_now(),
                        )
                        return command, conversation
                    raise RepositoryConflict("conversation is already terminal")
                if command_type in {"input", "approval"}:
                    pending = conversation_row.pending_interaction
                    if conversation_row.execution_state not in {
                        ExecutionState.WAITING_INPUT.value,
                        ExecutionState.PAUSED.value,
                    } or not pending:
                        raise RepositoryConflict("conversation is not waiting for input")
                    if interaction_id != pending.get("interaction_id"):
                        raise RepositoryConflict("interaction does not match pending interaction")
                command_row = RunCommandRow(
                    run_id=conversation_row.run_id,
                    type=command_type,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    state=CommandState.PENDING.value,
                    attempts=0,
                    created_at=_now(),
                )
                db.add(command_row)
                db.flush()
                created = True
            if created:
                job = db.execute(
                    select(ExecutionJobRow)
                    .where(ExecutionJobRow.run_id == conversation_row.run_id)
                    .with_for_update()
                ).scalar_one()
                now = _now()
                transition_type: str | None = None
                transition_payload: dict[str, Any] = {}
                transition_event_id: UUID | None = None
                if (
                    command_type in {"input", "approval"}
                    and conversation_row.execution_state == ExecutionState.PAUSED.value
                    and job.state == JobState.PAUSED.value
                ):
                    job.state = JobState.READY.value
                    job.available_at = now
                    job.state_changed_at = now
                    job.updated_at = now
                    conversation_row.execution_state = ExecutionState.RESUMING.value
                    transition_type = "run.resuming"
                    transition_payload = {"command_id": command_row.id}
                elif (
                    command_type == "cancel"
                    and job.state
                    in {JobState.READY.value, JobState.RETRY.value, JobState.PAUSED.value}
                    and job.claimed_by is None
                ):
                    job.state = JobState.CANCELLED.value
                    job.state_changed_at = now
                    job.updated_at = now
                    conversation_row.execution_state = ExecutionState.CANCELLED.value
                    conversation_row.pending_interaction = None
                    command_row.state = CommandState.APPLIED.value
                    transition_type = "run.cancelled"
                    transition_event_id = UUID(command_row.id)
                if transition_type:
                    seq = conversation_row.last_seq + 1
                    event = EventEnvelope(
                        event_id=transition_event_id or uuid4(),
                        run_id=UUID(conversation_row.run_id),
                        seq=seq,
                        type=transition_type,
                        payload=transition_payload,
                    )
                    db.add(
                        ConversationEventRow(
                            id=str(event.event_id),
                            conversation_id=conversation_row.id,
                            run_id=conversation_row.run_id,
                            seq=seq,
                            type=event.type,
                            payload=event.payload,
                            source=event.source,
                            occurred_at=event.occurred_at,
                        )
                    )
                    db.add(
                        OutboxEventRow(
                            topic="conversation.events",
                            aggregate_id=conversation_row.id,
                            payload=event.model_dump(mode="json"),
                            created_at=now,
                        )
                    )
                    conversation_row.last_seq = seq
            command = RunCommand(
                id=UUID(command_row.id),
                run_id=UUID(command_row.run_id),
                type=command_row.type,
                payload=command_row.payload,
                idempotency_key=command_row.idempotency_key,
                state=CommandState(command_row.state),
                claimed_by=command_row.claimed_by,
                attempts=command_row.attempts,
                created_at=_as_utc(command_row.created_at),
            )
            conversation = self.get_conversation(conversation_id, db=db)
            return command, conversation

    def append_claimed_event(
        self,
        run_id: UUID,
        worker_id: str,
        claim_token: int,
        event_type: str,
        payload: dict[str, Any],
        *,
        source: str = "agentsupport_worker",
        runner_seq: int | None = None,
        event_id: UUID | None = None,
    ) -> EventEnvelope:
        with self.transaction() as db:
            job = self._claimed_job(db, run_id, worker_id, claim_token)
            if event_id is not None:
                existing_by_id = db.get(ConversationEventRow, str(event_id))
                if existing_by_id:
                    return EventEnvelope(
                        event_id=UUID(existing_by_id.id),
                        run_id=UUID(existing_by_id.run_id),
                        seq=existing_by_id.seq,
                        type=existing_by_id.type,
                        payload=existing_by_id.payload,
                        source=existing_by_id.source,
                        occurred_at=_as_utc(existing_by_id.occurred_at),
                    )
            conversation = db.execute(
                select(ConversationRow)
                .where(ConversationRow.run_id == str(run_id))
                .with_for_update()
            ).scalar_one()
            if event_type == "run.started":
                conversation.execution_state = ExecutionState.STARTING.value
            elif event_type == "run.running":
                conversation.execution_state = ExecutionState.RUNNING.value
            elif event_type == "interaction.requested":
                conversation.execution_state = ExecutionState.WAITING_INPUT.value
                conversation.pending_interaction = payload
                job.state = JobState.WAITING.value
                job.state_changed_at = _now()
            elif event_type in {"interaction.input", "approval.decided", "run.resuming"}:
                conversation.execution_state = ExecutionState.RUNNING.value
                conversation.pending_interaction = None
                job.state = JobState.RUNNING.value
                job.state_changed_at = _now()
            elif event_type == "run.completed":
                conversation.execution_state = ExecutionState.COMPLETED.value
                conversation.pending_interaction = None
                conversation.result_summary = payload.get("result", payload)
            elif event_type == "run.failed":
                conversation.execution_state = ExecutionState.FAILED.value
                conversation.pending_interaction = None
            elif event_type == "run.cancelled":
                conversation.execution_state = ExecutionState.CANCELLED.value
                conversation.pending_interaction = None
            elif event_type == "run.lost":
                conversation.execution_state = ExecutionState.LOST.value
                conversation.pending_interaction = None
            seq = conversation.last_seq + 1
            event = EventEnvelope(
                event_id=event_id or uuid4(),
                run_id=run_id,
                seq=seq,
                type=event_type,
                payload=payload,
                source=source,
            )
            db.add(
                ConversationEventRow(
                    id=str(event.event_id),
                    conversation_id=conversation.id,
                    run_id=str(run_id),
                    seq=seq,
                    type=event.type,
                    payload=event.payload,
                    source=event.source,
                    occurred_at=event.occurred_at,
                    runner_seq=runner_seq,
                )
            )
            db.add(
                OutboxEventRow(
                    topic="conversation.events",
                    aggregate_id=conversation.id,
                    payload=event.model_dump(mode="json"),
                    created_at=_now(),
                )
            )
            conversation.last_seq = seq
            job.updated_at = _now()
            if runner_seq is not None:
                endpoint = db.get(RunnerEndpointRow, str(run_id), with_for_update=True)
                if endpoint:
                    endpoint.last_runner_seq = max(endpoint.last_runner_seq, runner_seq)
            return event

    def save_claimed_checkpoint(
        self,
        checkpoint: Checkpoint,
        worker_id: str,
        claim_token: int,
        *,
        reason: str,
    ) -> EventEnvelope:
        with self.transaction() as db:
            self._claimed_job(db, checkpoint.run_id, worker_id, claim_token)
            conversation = db.execute(
                select(ConversationRow)
                .where(ConversationRow.run_id == str(checkpoint.run_id))
                .with_for_update()
            ).scalar_one()
            db.merge(self._checkpoint_row(checkpoint))
            conversation.checkpoint_id = str(checkpoint.checkpoint_id)
            seq = conversation.last_seq + 1
            event = EventEnvelope(
                run_id=checkpoint.run_id,
                seq=seq,
                type="checkpoint.created",
                payload={
                    "checkpoint_id": str(checkpoint.checkpoint_id),
                    "reason": reason,
                },
                source="agentsupport_worker",
            )
            db.add(
                ConversationEventRow(
                    id=str(event.event_id),
                    conversation_id=conversation.id,
                    run_id=str(event.run_id),
                    seq=seq,
                    type=event.type,
                    payload=event.payload,
                    source=event.source,
                    occurred_at=event.occurred_at,
                )
            )
            db.add(
                OutboxEventRow(
                    topic="conversation.events",
                    aggregate_id=conversation.id,
                    payload=event.model_dump(mode="json"),
                    created_at=_now(),
                )
            )
            conversation.last_seq = seq
            return event

    def claim_pending_commands(
        self,
        run_id: UUID,
        worker_id: str,
        claim_token: int,
        *,
        limit: int = 10,
    ) -> list[RunCommand]:
        with self.transaction() as db:
            self._claimed_job(db, run_id, worker_id, claim_token)
            rows = list(
                db.execute(
                    select(RunCommandRow)
                    .where(
                        RunCommandRow.run_id == str(run_id),
                        or_(
                            RunCommandRow.state == CommandState.PENDING.value,
                            and_(
                                RunCommandRow.state == CommandState.CLAIMED.value,
                                RunCommandRow.claimed_by != worker_id,
                            ),
                        ),
                    )
                    .order_by(RunCommandRow.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                ).scalars()
            )
            result = []
            for row in rows:
                row.state = CommandState.CLAIMED.value
                row.claimed_by = worker_id
                row.attempts += 1
                result.append(
                    RunCommand(
                        id=UUID(row.id),
                        run_id=UUID(row.run_id),
                        type=row.type,
                        payload=row.payload,
                        idempotency_key=row.idempotency_key,
                        state=CommandState.CLAIMED,
                        claimed_by=worker_id,
                        attempts=row.attempts,
                        created_at=_as_utc(row.created_at),
                    )
                )
            return result

    def finish_command(
        self, command_id: UUID, worker_id: str, *, succeeded: bool
    ) -> None:
        with self.transaction() as db:
            row = db.get(RunCommandRow, str(command_id), with_for_update=True)
            if row is None:
                raise RepositoryConflict("run command does not exist")
            if row.claimed_by != worker_id or row.state != CommandState.CLAIMED.value:
                raise StaleClaim("run command claim is stale")
            row.state = (
                CommandState.APPLIED.value if succeeded else CommandState.PENDING.value
            )
            row.claimed_by = None

    def claim_outbox(
        self,
        publisher_id: str,
        *,
        limit: int = 100,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> list[OutboxNotification]:
        claimed_at = now or _now()
        expires_at = claimed_at + timedelta(seconds=lease_seconds)
        with self.transaction() as db:
            rows = list(
                db.execute(
                    select(OutboxEventRow)
                    .where(
                        OutboxEventRow.published_at.is_(None),
                        or_(
                            OutboxEventRow.claimed_by.is_(None),
                            OutboxEventRow.claim_expires_at < claimed_at,
                        ),
                    )
                    .order_by(OutboxEventRow.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                ).scalars()
            )
            result = []
            for row in rows:
                row.claimed_by = publisher_id
                row.claim_expires_at = expires_at
                row.attempts += 1
                result.append(
                    OutboxNotification(
                        id=UUID(row.id),
                        topic=row.topic,
                        aggregate_id=UUID(row.aggregate_id),
                        payload=row.payload,
                        attempts=row.attempts,
                        created_at=_as_utc(row.created_at),
                    )
                )
            return result

    def finish_outbox(
        self, notification_id: UUID, publisher_id: str, *, published: bool
    ) -> None:
        with self.transaction() as db:
            row = db.get(OutboxEventRow, str(notification_id), with_for_update=True)
            if row is None:
                raise RepositoryConflict("outbox event does not exist")
            if row.claimed_by != publisher_id:
                raise StaleClaim("outbox claim is stale")
            if published:
                row.published_at = _now()
            row.claimed_by = None
            row.claim_expires_at = None

    def retention_cleanup(
        self,
        *,
        idempotency_hours: int = 24,
        unreferenced_checkpoints_hours: int = 24,
        published_outbox_days: int = 7,
        terminal_jobs_days: int = 30,
        terminal_events_days: int = 90,
    ) -> dict[str, int]:
        """Delete coordination rows that outlived their retention windows.

        Each window of ``0`` disables cleanup for that table. Authoritative
        resources (workspaces, sessions, conversations, users, projects) are
        never deleted here; only transient coordination state is.
        """

        now = _now()
        result: dict[str, int] = {}
        with self.transaction() as db:
            if idempotency_hours > 0:
                cutoff = now - timedelta(hours=idempotency_hours)
                result["idempotency_keys"] = db.execute(
                    delete(IdempotencyKeyRow).where(IdempotencyKeyRow.created_at < cutoff)
                ).rowcount
            if unreferenced_checkpoints_hours > 0:
                cutoff = now - timedelta(hours=unreferenced_checkpoints_hours)
                referenced = select(ConversationRow.checkpoint_id).where(
                    ConversationRow.checkpoint_id.is_not(None)
                )
                result["conversation_checkpoints"] = db.execute(
                    delete(ConversationCheckpointRow).where(
                        ConversationCheckpointRow.created_at < cutoff,
                        ConversationCheckpointRow.id.not_in(referenced),
                    )
                ).rowcount
            if published_outbox_days > 0:
                cutoff = now - timedelta(days=published_outbox_days)
                result["outbox_events"] = db.execute(
                    delete(OutboxEventRow).where(
                        OutboxEventRow.published_at.is_not(None),
                        OutboxEventRow.published_at < cutoff,
                    )
                ).rowcount
            if terminal_jobs_days > 0:
                cutoff = now - timedelta(days=terminal_jobs_days)
                terminal_run_ids = select(ExecutionJobRow.run_id).where(
                    ExecutionJobRow.state.in_(
                        [
                            JobState.COMPLETED.value,
                            JobState.FAILED.value,
                            JobState.CANCELLED.value,
                        ]
                    ),
                    ExecutionJobRow.updated_at < cutoff,
                )
                result["execution_jobs"] = db.execute(
                    delete(ExecutionJobRow).where(ExecutionJobRow.run_id.in_(terminal_run_ids))
                ).rowcount
                result["runner_endpoints"] = db.execute(
                    delete(RunnerEndpointRow).where(RunnerEndpointRow.run_id.in_(terminal_run_ids))
                ).rowcount
                result["run_commands"] = db.execute(
                    delete(RunCommandRow).where(
                        RunCommandRow.state.in_(
                            [CommandState.APPLIED.value, CommandState.FAILED.value]
                        ),
                        RunCommandRow.created_at < cutoff,
                    )
                ).rowcount
                result["runtime_operations"] = db.execute(
                    delete(RuntimeOperationRow).where(
                        RuntimeOperationRow.status.in_(["SUCCEEDED", "FAILED"]),
                        RuntimeOperationRow.created_at < cutoff,
                    )
                ).rowcount
                result["container_leases"] = db.execute(
                    delete(ContainerLeaseRow).where(
                        ContainerLeaseRow.status != "ACTIVE",
                        ContainerLeaseRow.expires_at.is_not(None),
                        ContainerLeaseRow.expires_at < cutoff,
                    )
                ).rowcount
                result["workspace_write_leases"] = db.execute(
                    delete(WorkspaceWriteLeaseRow).where(
                        WorkspaceWriteLeaseRow.status != "ACTIVE",
                        WorkspaceWriteLeaseRow.expires_at.is_not(None),
                        WorkspaceWriteLeaseRow.expires_at < cutoff,
                    )
                ).rowcount
            if terminal_events_days > 0:
                cutoff = now - timedelta(days=terminal_events_days)
                terminal_conversation_ids = select(ConversationRow.id).where(
                    ConversationRow.execution_state.in_(
                        [
                            ExecutionState.COMPLETED.value,
                            ExecutionState.FAILED.value,
                            ExecutionState.CANCELLED.value,
                            ExecutionState.LOST.value,
                        ]
                    ),
                    ConversationRow.created_at < cutoff,
                )
                result["conversation_events"] = db.execute(
                    delete(ConversationEventRow).where(
                        ConversationEventRow.conversation_id.in_(terminal_conversation_ids)
                    )
                ).rowcount
        return result
