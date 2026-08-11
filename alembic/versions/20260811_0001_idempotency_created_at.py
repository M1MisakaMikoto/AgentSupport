"""Add created_at to idempotency_keys for retention cleanup.

Revision ID: 20260811_0001
Revises: 20260804_0001
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260811_0001"
down_revision: str | None = "20260804_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("idempotency_keys")}
    if "created_at" in columns:
        return
    column = sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("idempotency_keys") as batch:
            batch.add_column(column)
    else:
        op.add_column("idempotency_keys", column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("idempotency_keys")}
    if "created_at" not in columns:
        return
    op.drop_column("idempotency_keys", "created_at")
