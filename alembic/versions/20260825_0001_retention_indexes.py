"""Add indexes for retention cleanup predicates.

The retention process deletes by ``created_at`` / ``published_at`` /
``expires_at`` on six tables. Without indexes those DELETEs are full table
scans once the tables grow (verified by the exp7_indexes experiment).

Revision ID: 20260825_0001
Revises: 20260819_0001
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260825_0001"
down_revision: str | None = "20260819_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # The first migration bootstraps the schema with ``Base.metadata.create_all``
    # on fresh installs, so these indexes may already exist there; guard each
    # one so both fresh and upgraded databases converge.
    for index, table, column in (
        ("ix_outbox_events_published_at", "outbox_events", "published_at"),
        ("ix_idempotency_keys_created_at", "idempotency_keys", "created_at"),
        (
            "ix_conversation_checkpoints_created_at",
            "conversation_checkpoints",
            "created_at",
        ),
        ("ix_runtime_operations_created_at", "runtime_operations", "created_at"),
        ("ix_container_leases_expires_at", "container_leases", "expires_at"),
        (
            "ix_workspace_write_leases_expires_at",
            "workspace_write_leases",
            "expires_at",
        ),
    ):
        existing = {
            index_info["name"]
            for index_info in inspector.get_indexes(table)
        }
        if index not in existing:
            op.create_index(index, table, [column])


def downgrade() -> None:
    for index, table in (
        ("ix_outbox_events_published_at", "outbox_events"),
        ("ix_idempotency_keys_created_at", "idempotency_keys"),
        ("ix_conversation_checkpoints_created_at", "conversation_checkpoints"),
        ("ix_runtime_operations_created_at", "runtime_operations"),
        ("ix_container_leases_expires_at", "container_leases"),
        ("ix_workspace_write_leases_expires_at", "workspace_write_leases"),
    ):
        op.drop_index(index, table_name=table)
