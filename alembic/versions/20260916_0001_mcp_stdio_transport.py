"""Add stdio transport fields to ``mcp_servers``.

trae mode's vendored MCP client only implements **stdio** transport (an ``http_url``
registration raises ``NotImplementedError`` inside ``discover_mcp_tools``, which is
swallowed silently). Registering the tool server as a stdio child process keeps the
per-session ``mcp_refs`` path working.

Revision ID: 20260916_0001
Revises: 20260914_0001
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260916_0001"
down_revision: str | None = "20260914_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "mcp_servers" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("mcp_servers")}
    if "command" not in columns:
        op.add_column("mcp_servers", sa.Column("command", sa.Text(), nullable=True))
    if "args" not in columns:
        op.add_column(
            "mcp_servers",
            sa.Column("args", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )
    if "env" not in columns:
        op.add_column(
            "mcp_servers",
            sa.Column("env", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )
    if "cwd" not in columns:
        op.add_column("mcp_servers", sa.Column("cwd", sa.Text(), nullable=True))


def downgrade() -> None:
    for name in ("cwd", "env", "args", "command"):
        op.drop_column("mcp_servers", name)
