"""Add skill generation tables.

``skill_generation_requests`` tracks manual generation requests (each backed
by an agent run); ``skill_drafts`` holds generated SKILL.md content awaiting
human review before publication into the tenant-scoped skill library.

Revision ID: 20260828_0001
Revises: 20260826_0001
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260828_0001"
down_revision: str | None = "20260826_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.Column:
    return sa.Column("id", sa.String(36), primary_key=True)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "skill_generation_requests" not in existing:
        op.create_table(
            "skill_generation_requests",
            _uuid(),
            sa.Column("session_id", sa.String(36), nullable=False),
            sa.Column("conversation_id", sa.String(36), nullable=True),
            sa.Column("tenant_id", sa.String(120), nullable=True),
            sa.Column("project_id", sa.String(120), nullable=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_skill_generation_requests_session_id",
            "skill_generation_requests",
            ["session_id"],
        )
        op.create_index(
            "ix_skill_generation_requests_conversation_id",
            "skill_generation_requests",
            ["conversation_id"],
        )
        op.create_index(
            "ix_skill_generation_requests_tenant_id",
            "skill_generation_requests",
            ["tenant_id"],
        )

    if "skill_drafts" not in existing:
        op.create_table(
            "skill_drafts",
            _uuid(),
            sa.Column("skill_id", sa.String(120), nullable=False),
            sa.Column("tenant_id", sa.String(120), nullable=True),
            sa.Column("project_id", sa.String(120), nullable=True),
            sa.Column("source_session_id", sa.String(36), nullable=False),
            sa.Column("source_conversation_id", sa.String(36), nullable=False),
            sa.Column("generation_id", sa.String(36), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("skill_content", sa.Text(), nullable=False),
            sa.Column("frontmatter", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("review_note", sa.Text(), nullable=True),
        )
        op.create_index(
            "ix_skill_drafts_tenant_id",
            "skill_drafts",
            ["tenant_id"],
        )
        op.create_index(
            "ix_skill_drafts_project_id",
            "skill_drafts",
            ["project_id"],
        )


def downgrade() -> None:
    op.drop_table("skill_drafts")
    op.drop_table("skill_generation_requests")
