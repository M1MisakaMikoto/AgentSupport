"""Persist conversation execution mode.

The control plane now runs on the Temporal-only core; conversation mode
(default/silent/no_approval) must survive repository round-trips so silent
skill-generation runs are not lost on reload.

Revision ID: 20260902_0001
Revises: 20260828_0001
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_0001"
down_revision: str | None = "20260828_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "conversations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "mode" not in columns:
        op.add_column(
            "conversations",
            sa.Column(
                "mode",
                sa.String(32),
                nullable=False,
                server_default="default",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "conversations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "mode" in columns:
        op.drop_column("conversations", "mode")
