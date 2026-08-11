"""Add session labels/config and drop business entity tables.

Revision ID: 20260811_0003
Revises: 20260811_0002
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260811_0003"
down_revision: str | None = "20260811_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}

    for name, column in {
        "tenant_id": sa.Column("tenant_id", sa.String(120), nullable=True),
        "user_id": sa.Column("user_id", sa.String(120), nullable=True),
        "metadata": sa.Column(
            "metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        "config": sa.Column("config", sa.JSON(), nullable=True),
    }.items():
        if name not in session_columns:
            op.add_column("sessions", column)

    # Backfill labels from the soon-to-be-dropped business tables. IDs become
    # opaque labels; missing sources stay NULL (default/unknown bucket).
    if {"projects", "users", "organizations"} <= tables:
        op.execute(
            "UPDATE sessions SET user_id = (SELECT user_id FROM projects "
            "WHERE projects.id = sessions.project_id) "
            "WHERE sessions.user_id IS NULL AND sessions.project_id IS NOT NULL"
        )
        op.execute(
            "UPDATE sessions SET tenant_id = (SELECT organization_id FROM users "
            "WHERE users.id = sessions.user_id) "
            "WHERE sessions.tenant_id IS NULL AND sessions.user_id IS NOT NULL"
        )

    for table in ("presets", "projects", "users", "organizations"):
        if table in tables:
            op.drop_table(table)

    workspace_columns = {
        column["name"] for column in inspector.get_columns("workspaces")
    }
    if "organization_id" in workspace_columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("workspaces") as batch:
                batch.drop_column("organization_id")
        else:
            op.drop_column("workspaces", "organization_id")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "organizations" not in tables:
        op.create_table(
            "organizations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("organization_id", sa.String(36), nullable=False),
            sa.Column("username", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "projects" not in tables:
        op.create_table(
            "projects",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("organization_id", sa.String(36), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("workspace_id", sa.String(36), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("preset_id", sa.String(36), nullable=True),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    workspace_columns = {
        column["name"] for column in inspector.get_columns("workspaces")
    }
    if "organization_id" not in workspace_columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("workspaces") as batch:
                batch.add_column(
                    sa.Column("organization_id", sa.String(36), nullable=True)
                )
        else:
            op.add_column(
                "workspaces",
                sa.Column("organization_id", sa.String(36), nullable=True),
            )
