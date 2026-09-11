"""Drop the retired per-conversation skill list.

Conversations no longer pick skills: the candidate pool now lives on the
session/project config and the agent reads skill bodies on demand, so the
column is dead weight.

Revision ID: 20260911_0001
Revises: 20260902_0001
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260911_0001"
down_revision: str | None = "20260902_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "conversations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "skills" in columns:
        op.drop_column("conversations", "skills")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "conversations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "skills" not in columns:
        op.add_column("conversations", sa.Column("skills", sa.JSON(), nullable=True))
