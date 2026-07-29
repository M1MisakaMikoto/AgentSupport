"""Track execution job state age independently from heartbeat updates.

Revision ID: 20260728_0002
Revises: 20260728_0001
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260728_0002"
down_revision: str | None = "20260728_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {
        column["name"] for column in sa.inspect(bind).get_columns("execution_jobs")
    }
    if "state_changed_at" in existing:
        return
    op.add_column(
        "execution_jobs",
        sa.Column(
            "state_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    if bind.dialect.name != "sqlite":
        op.alter_column("execution_jobs", "state_changed_at", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    existing = {
        column["name"] for column in sa.inspect(bind).get_columns("execution_jobs")
    }
    if "state_changed_at" in existing:
        op.drop_column("execution_jobs", "state_changed_at")
