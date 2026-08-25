from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, func, inspect, or_, select, text
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
    McpServer,
    OutboxNotification,
    PresetSkill,
    ProjectConfig,
    RunProjection,
    Session,
    Workspace,
)
from .models import (
    Base,
    ContainerLeaseRow,
    ConversationCheckpointRow,
    ConversationEventRow,
    ConversationRow,
    IdempotencyKeyRow,
    McpServerRow,
    OutboxEventRow,
    RunnerRegistrationRow,
    RuntimeOperationRow,
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

    def list_workspaces(
        self, *, limit: int | None = None, offset: int = 0
    ) -> list[Workspace]:
        with self.transaction() as db:
            statement = select(WorkspaceRow).order_by(WorkspaceRow.created_at)
            if limit is not None:
                statement = statement.limit(limit).offset(offset)
            rows = db.execute(statement).scalars()
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
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Session]:
        with self.transaction() as db:
            statement = select(SessionRow).order_by(SessionRow.created_at)
            if tenant_id is not None:
                statement = statement.where(SessionRow.tenant_id == tenant_id)
            if user_id is not None:
                statement = statement.where(SessionRow.user_id == user_id)
            if project_id is not None:
                statement = statement.where(SessionRow.project_id == str(project_id))
            if limit is not None:
                statement = statement.limit(limit).offset(offset)
            rows = list(db.execute(statement).scalars())
            active_leases = {
                lease.session_id: lease
                for lease in db.execute(
                    select(ContainerLeaseRow).where(ContainerLeaseRow.status == "ACTIVE")
                ).scalars()
            }
            result: list[Session] = []
            for row in rows:
                lease = active_leases.get(row.id)
                result.append(
                    Session(
                        id=UUID(row.id),
                        workspace_id=UUID(row.workspace_id),
                        tenant_id=row.tenant_id,
                        user_id=row.user_id,
                        project_id=row.project_id,
                        metadata=dict(row.labels),
                        config=ProjectConfig.model_validate(row.config) if row.config else None,
                        lease_epoch=row.lease_epoch,
                        active_run_id=UUID(row.active_run_id) if row.active_run_id else None,
                        active_container_id=lease.container_id if lease else None,
                    )
                )
            return result

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
                skills=(
                    [skill.model_dump(mode="json") for skill in conversation.skills]
                    if conversation.skills is not None
                    else None
                ),
                mcp_refs=(
                    [dict(item) for item in conversation.mcp_refs]
                    if conversation.mcp_refs is not None
                    else None
                ),
                execution_state=conversation.run.state.value,
                run_id=str(conversation.run.run_id),
                last_seq=conversation.run.last_seq,
                pending_interaction=conversation.run.pending_interaction,
                error=conversation.run.error,
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
        return self._conversation_from_row(row)

    @staticmethod
    def _conversation_from_row(row: ConversationRow) -> Conversation:
        return Conversation(
            id=UUID(row.id),
            session_id=UUID(row.session_id),
            parent_conversation_id=(
                UUID(row.parent_conversation_id) if row.parent_conversation_id else None
            ),
            task=row.task,
            skills=(
                [PresetSkill.model_validate(item) for item in row.skills]
                if row.skills is not None
                else None
            ),
            mcp_refs=(
                [dict(item) for item in row.mcp_refs]
                if row.mcp_refs is not None
                else None
            ),
            created_at=row.created_at,
            run=RunProjection(
                run_id=UUID(row.run_id),
                state=ExecutionState(row.execution_state),
                last_seq=row.last_seq,
                pending_interaction=row.pending_interaction,
                error=row.error,
                result_summary=row.result_summary,
                checkpoint_id=UUID(row.checkpoint_id) if row.checkpoint_id else None,
            ),
        )

    def list_conversations(
        self,
        session_id: UUID | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Conversation]:
        with self.transaction() as db:
            statement = select(ConversationRow).order_by(ConversationRow.created_at)
            if session_id is not None:
                statement = statement.where(
                    ConversationRow.session_id == str(session_id)
                )
            if limit is not None:
                statement = statement.limit(limit).offset(offset)
            return [
                self._conversation_from_row(row)
                for row in db.execute(statement).scalars()
            ]

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
            row.error = conversation.run.error
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

    def list_events(
        self,
        conversation_id: UUID,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        with self.transaction() as db:
            statement = (
                select(ConversationEventRow)
                .where(
                    ConversationEventRow.conversation_id == str(conversation_id),
                    ConversationEventRow.seq > after_seq,
                )
                .order_by(ConversationEventRow.seq)
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = db.execute(statement).scalars()
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

    def list_session_events(
        self, session_id: UUID, after_seq: int = 0, limit: int | None = None
    ) -> list[EventEnvelope]:
        """All events of a session's conversations in a single query.

        ``after_seq`` mirrors the per-conversation semantics of
        ``list_events``: only events with ``seq > after_seq`` are returned
        (``seq`` is unique per conversation, so the union of per-conversation
        filters equals a global ``seq > after_seq`` filter).
        """

        with self.transaction() as db:
            statement = (
                select(ConversationEventRow)
                .join(
                    ConversationRow,
                    ConversationRow.id == ConversationEventRow.conversation_id,
                )
                .where(
                    ConversationRow.session_id == str(session_id),
                    ConversationEventRow.seq > after_seq,
                )
                .order_by(ConversationEventRow.occurred_at, ConversationEventRow.seq)
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = db.execute(statement).scalars()
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
            row.error = conversation.run.error
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

    def conversation_state_counts(self) -> dict[str, int]:
        """Number of conversations per execution state (single query)."""

        with self.transaction() as db:
            rows = db.execute(
                select(ConversationRow.execution_state, func.count()).group_by(
                    ConversationRow.execution_state
                )
            ).all()
        return {state: int(count) for state, count in rows}

    def oldest_queued_created_at(self) -> datetime | None:
        with self.transaction() as db:
            return db.execute(
                select(func.min(ConversationRow.created_at)).where(
                    ConversationRow.execution_state == ExecutionState.QUEUED.value
                )
            ).scalar_one_or_none()

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

    def save_mcp_server(self, server: McpServer) -> None:
        with self.transaction() as db:
            row = db.get(McpServerRow, server.server_id)
            if row is None:
                db.add(
                    McpServerRow(
                        server_id=server.server_id,
                        name=server.name,
                        transport=server.transport,
                        http_url=server.http_url,
                        sse_url=server.sse_url,
                        headers=dict(server.headers),
                        description=server.description,
                        enabled=server.enabled,
                        created_at=server.created_at,
                        updated_at=server.updated_at,
                    )
                )
                return
            row.name = server.name
            row.transport = server.transport
            row.http_url = server.http_url
            row.sse_url = server.sse_url
            row.headers = dict(server.headers)
            row.description = server.description
            row.enabled = server.enabled
            row.updated_at = server.updated_at

    def get_mcp_server(self, server_id: str) -> McpServer | None:
        with self.transaction() as db:
            row = db.get(McpServerRow, server_id)
            return self._mcp_server_from_row(row) if row is not None else None

    def list_mcp_servers(self) -> list[McpServer]:
        with self.transaction() as db:
            rows = db.execute(select(McpServerRow).order_by(McpServerRow.server_id)).scalars()
            return [self._mcp_server_from_row(row) for row in rows]

    def delete_mcp_server(self, server_id: str) -> bool:
        with self.transaction() as db:
            row = db.get(McpServerRow, server_id)
            if row is None:
                return False
            db.delete(row)
            return True

    @staticmethod
    def _mcp_server_from_row(row: McpServerRow) -> McpServer:
        return McpServer(
            server_id=row.server_id,
            name=row.name,
            transport=row.transport,
            http_url=row.http_url,
            sse_url=row.sse_url,
            headers=dict(row.headers),
            description=row.description,
            enabled=row.enabled,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )




    def health_check(self) -> None:
        with self.transaction() as db:
            db.execute(text("SELECT 1"))








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

    def outbox_stats(self, *, now: datetime | None = None) -> dict[str, float]:
        """Pending outbox count and age of the oldest pending item (seconds)."""

        current = now or _now()
        with self.transaction() as db:
            pending = int(
                db.execute(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(OutboxEventRow.published_at.is_(None))
                ).scalar_one()
            )
            oldest = db.execute(
                select(func.min(OutboxEventRow.created_at)).where(
                    OutboxEventRow.published_at.is_(None)
                )
            ).scalar_one()
        lag_seconds = 0.0
        if oldest is not None:
            lag_seconds = max(0.0, (current - _as_utc(oldest)).total_seconds())
        return {"pending": float(pending), "lag_seconds": round(lag_seconds, 3)}

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
        lease_retention_days: int = 30,
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
            if lease_retention_days > 0:
                lease_cutoff = now - timedelta(days=lease_retention_days)
                result["runtime_operations"] = db.execute(
                    delete(RuntimeOperationRow).where(
                        RuntimeOperationRow.status.in_(["SUCCEEDED", "FAILED"]),
                        RuntimeOperationRow.created_at < lease_cutoff,
                    )
                ).rowcount
                result["container_leases"] = db.execute(
                    delete(ContainerLeaseRow).where(
                        ContainerLeaseRow.status != "ACTIVE",
                        ContainerLeaseRow.expires_at.is_not(None),
                        ContainerLeaseRow.expires_at < lease_cutoff,
                    )
                ).rowcount
                result["workspace_write_leases"] = db.execute(
                    delete(WorkspaceWriteLeaseRow).where(
                        WorkspaceWriteLeaseRow.status != "ACTIVE",
                        WorkspaceWriteLeaseRow.expires_at.is_not(None),
                        WorkspaceWriteLeaseRow.expires_at < lease_cutoff,
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
