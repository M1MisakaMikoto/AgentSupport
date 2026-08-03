"""Add durable distributed coordination schema.

Revision ID: 20260728_0001
Revises:
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from agentsupport.db import Base
from alembic import op

revision: str = "20260728_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_missing_columns(table: str, columns: dict[str, sa.Column]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    _add_missing_columns(
        "sessions",
        {"active_run_id": sa.Column("active_run_id", sa.String(36), nullable=True)},
    )
    _add_missing_columns(
        "conversations",
        {"checkpoint_id": sa.Column("checkpoint_id", sa.String(36), nullable=True)},
    )
    _add_missing_columns(
        "conversation_events",
        {"runner_seq": sa.Column("runner_seq", sa.Integer(), nullable=True)},
    )
    lease_columns = {
        "run_id": sa.Column("run_id", sa.String(36), nullable=True),
        "owner_instance_id": sa.Column("owner_instance_id", sa.String(160), nullable=True),
        "heartbeat_at": sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        "expires_at": sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    }
    _add_missing_columns("container_leases", lease_columns)
    _add_missing_columns(
        "workspace_write_leases",
        {
            name: sa.Column(name, column.type, nullable=True)
            for name, column in lease_columns.items()
        },
    )
    _add_missing_columns(
        "conversation_checkpoints",
        {
            "core_type": sa.Column("core_type", sa.String(40), nullable=True),
            "core_version": sa.Column("core_version", sa.String(80), nullable=True),
            "tool_policy_hash": sa.Column("tool_policy_hash", sa.String(64), nullable=True),
            "tool_versions_hash": sa.Column("tool_versions_hash", sa.String(64), nullable=True),
        },
    )
def downgrade() -> None:
    for table in [
        "outbox_events",
        "run_commands",
        "runner_endpoints",
        "runtime_slots",
        "execution_jobs",
    ]:
        op.drop_table(table)
