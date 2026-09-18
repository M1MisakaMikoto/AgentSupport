"""Add tenant presets and their asynchronous image builds.

``tenant_presets`` stores the configuration half of a tenant (CLI apps, skills,
environment variable names). It is global storage keyed by ``tenant_id`` with
no authentication, matching how sessions carry tenant labels.
``tenant_preset_builds`` tracks the runner image build triggered by an upload.

Revision ID: 20260914_0001
Revises: 20260911_0001
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260914_0001"
down_revision: str | None = "20260911_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "tenant_presets" not in existing:
        op.create_table(
            "tenant_presets",
            sa.Column("tenant_id", sa.String(120), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("cli_apps", sa.JSON(), nullable=False),
            sa.Column("skills", sa.JSON(), nullable=False),
            sa.Column("env", sa.JSON(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "tenant_preset_builds" not in existing:
        op.create_table(
            "tenant_preset_builds",
            sa.Column("build_id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(120), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("image_tag", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("log_tail", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_tenant_preset_builds_tenant_id",
            "tenant_preset_builds",
            ["tenant_id"],
        )
        op.create_index(
            "ix_tenant_preset_builds_status",
            "tenant_preset_builds",
            ["status"],
        )
        op.create_index(
            "ix_tenant_preset_builds_created_at",
            "tenant_preset_builds",
            ["created_at"],
        )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "tenant_preset_builds" in existing:
        op.drop_table("tenant_preset_builds")
    if "tenant_presets" in existing:
        op.drop_table("tenant_presets")
