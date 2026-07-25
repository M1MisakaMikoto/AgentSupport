from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, func, inspect, select, text
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from .db import (
    Base,
    ContainerLeaseRow,
    ConversationCheckpointRow,
    ConversationEventRow,
    ConversationRow,
    IdempotencyKeyRow,
    OrganizationRow,
    RuntimeOperationRow,
    SessionRow,
    WorkspaceRow,
    WorkspaceWriteLeaseRow,
)
from .domain import (
    Checkpoint,
    ContextBundle,
    Conversation,
    ExecutionState,
    RunProjection,
    Session,
    Workspace,
)
from .events import EventEnvelope


def _now() -> datetime:
    return datetime.now(UTC)


class RepositoryConflict(Exception):
    pass


class PostgresRepository:
    """SQLAlchemy repository for the first-release PostgreSQL persistence boundary."""

    def __init__(self, database_url: str, *, create_schema: bool = False) -> None:
        self.engine = create_engine(database_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        self.organization_id = uuid4()
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
        self._ensure_organization()

    @contextmanager
    def transaction(self) -> Iterator[DbSession]:
        db = self.session_factory()
        try:
            with db.begin():
                yield db
        finally:
            db.close()

    def _ensure_organization(self) -> None:
        with self.transaction() as db:
            existing = db.execute(select(OrganizationRow).limit(1)).scalar_one_or_none()
            if existing:
                self.organization_id = UUID(existing.id)
                return
            db.add(OrganizationRow(id=str(self.organization_id), name="default", created_at=_now()))

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
            existing = self._idempotent_resource(db, scope, key, request_hash)
            if existing:
                return
            self._save_idempotency(db, scope, key, request_hash, resource_id, response_payload)

    def create_workspace(
        self, name: str, root_path: str, request_hash: str, idempotency_key: str | None
    ) -> Workspace:
        with self.transaction() as db:
            existing = self._idempotent_resource(db, "workspace", idempotency_key, request_hash)
            if existing:
                return self.get_workspace(existing, db=db)
            workspace = Workspace(name=name, root_path=root_path)
            db.add(
                WorkspaceRow(
                    id=str(workspace.id),
                    organization_id=str(self.organization_id),
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
        self, workspace: Workspace, request_hash: str, idempotency_key: str | None
    ) -> Session:
        with self.transaction() as db:
            existing = self._idempotent_resource(db, "session", idempotency_key, request_hash)
            if existing:
                return self.get_session(existing, db=db)
            session = Session(workspace_id=workspace.id)
            db.add(
                SessionRow(
                    id=str(session.id),
                    workspace_id=str(session.workspace_id),
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
            lease_epoch=row.lease_epoch,
            active_run_id=UUID(row.active_run_id) if row.active_run_id else None,
            active_container_id=active.container_id if active else None,
        )

    def list_sessions(self) -> list[Session]:
        with self.transaction() as db:
            rows = db.execute(select(SessionRow).order_by(SessionRow.created_at)).scalars()
            return [self.get_session(UUID(row.id), db=db) for row in rows]

    def save_session(self, session: Session) -> None:
        with self.transaction() as db:
            row = db.get(SessionRow, str(session.id))
            if not row:
                raise RepositoryConflict("session does not exist")
            row.lease_epoch = session.lease_epoch
            row.active_run_id = str(session.active_run_id) if session.active_run_id else None

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

    def list_conversations(self) -> list[Conversation]:
        with self.transaction() as db:
            rows = db.execute(
                select(ConversationRow).order_by(ConversationRow.created_at)
            ).scalars()
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
