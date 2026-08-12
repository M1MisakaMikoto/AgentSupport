"""Add mcp_servers registration table and conversation-level mcp_refs.

Revision ID: 20260812_0002
Revises: 20260812_0001
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260812_0002"
down_revision: str | None = "20260812_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "mcp_servers" not in tables:
        op.create_table(
            "mcp_servers",
            sa.Column("server_id", sa.String(120), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("transport", sa.String(16), nullable=False),
            sa.Column("http_url", sa.Text(), nullable=True),
            sa.Column("sse_url", sa.Text(), nullable=True),
            sa.Column("headers", sa.JSON(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    conversation_columns = {
        column["name"] for column in inspector.get_columns("conversations")
    }
    if "mcp_refs" not in conversation_columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("conversations") as batch:
                batch.add_column(sa.Column("mcp_refs", sa.JSON(), nullable=True))
        else:
            op.add_column("conversations", sa.Column("mcp_refs", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    conversation_columns = {
        column["name"] for column in inspector.get_columns("conversations")
    }
    if "mcp_refs" in conversation_columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("conversations") as batch:
                batch.drop_column("mcp_refs")
        else:
            op.drop_column("conversations", "mcp_refs")
    tables = set(inspector.get_table_names())
    if "mcp_servers" in tables:
        op.drop_table("mcp_servers")
