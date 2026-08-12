"""Add conversation-level skills column for per-conversation skill activation.

Revision ID: 20260812_0001
Revises: 20260811_0003
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260812_0001"
down_revision: str | None = "20260811_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "skills" in columns:
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("conversations") as batch:
            batch.add_column(sa.Column("skills", sa.JSON(), nullable=True))
    else:
        op.add_column("conversations", sa.Column("skills", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "skills" not in columns:
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("conversations") as batch:
            batch.drop_column("skills")
    else:
        op.drop_column("conversations", "skills")
