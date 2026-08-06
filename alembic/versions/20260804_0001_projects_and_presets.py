"""Add projects and presets; link sessions to projects.

Revision ID: 20260804_0001
Revises: 20260728_0003
Create Date: 2026-08-04
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260804_0001"
down_revision: str | None = "20260728_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    session_columns = {
        column["name"] for column in inspector.get_columns("sessions")
    }

    if "presets" not in existing_tables:
        op.create_table(
            "presets",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("definition", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_presets_organization_id", "presets", ["organization_id"])
        op.create_index("ix_presets_user_id", "presets", ["user_id"])

    if "projects" not in existing_tables:
        op.create_table(
            "projects",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("workspace_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("preset_id", sa.String(length=36), nullable=True),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_projects_organization_id", "projects", ["organization_id"])
        op.create_index("ix_projects_user_id", "projects", ["user_id"])
        op.create_index("ix_projects_workspace_id", "projects", ["workspace_id"], unique=True)
        op.create_index("ix_projects_preset_id", "projects", ["preset_id"])

    if "project_id" not in session_columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("sessions") as batch:
                batch.add_column(
                    sa.Column("project_id", sa.String(length=36), nullable=True)
                )
                batch.create_index("ix_sessions_project_id", ["project_id"])
        else:
            op.add_column(
                "sessions", sa.Column("project_id", sa.String(length=36), nullable=True)
            )
            op.create_index("ix_sessions_project_id", "sessions", ["project_id"])

    _backfill_projects(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    session_columns = {
        column["name"] for column in inspector.get_columns("sessions")
    }
    if "project_id" in session_columns:
        op.drop_index("ix_sessions_project_id", table_name="sessions")
        op.drop_column("sessions", "project_id")
    existing_tables = set(inspector.get_table_names())
    if "projects" in existing_tables:
        op.drop_index("ix_projects_preset_id", table_name="projects")
        op.drop_index("ix_projects_workspace_id", table_name="projects")
        op.drop_index("ix_projects_user_id", table_name="projects")
        op.drop_index("ix_projects_organization_id", table_name="projects")
        op.drop_table("projects")
    if "presets" in existing_tables:
        op.drop_index("ix_presets_user_id", table_name="presets")
        op.drop_index("ix_presets_organization_id", table_name="presets")
        op.drop_table("presets")


def _backfill_projects(bind) -> None:
    rows = bind.execute(sa.text("SELECT id, name FROM organizations")).fetchall()
    for org_id, _org_name in rows:
        users = bind.execute(
            sa.text("SELECT id FROM users WHERE organization_id = :org_id ORDER BY created_at"),
            {"org_id": org_id},
        ).fetchall()
        if not users:
            user_id = str(uuid.uuid4())
            username = _default_username(bind, org_id)
            bind.execute(
                sa.text(
                    "INSERT INTO users (id, organization_id, username, created_at) "
                    "VALUES (:id, :org_id, :username, CURRENT_TIMESTAMP)"
                ),
                {"id": user_id, "org_id": org_id, "username": username},
            )
            users = [(user_id,)]
        owner_id = users[0][0]
        workspaces = bind.execute(
            sa.text("SELECT id, name FROM workspaces WHERE organization_id = :org_id"),
            {"org_id": org_id},
        ).fetchall()
        for workspace_id, workspace_name in workspaces:
            existing = bind.execute(
                sa.text("SELECT id FROM projects WHERE workspace_id = :wid"),
                {"wid": workspace_id},
            ).fetchone()
            if existing:
                project_id = existing[0]
            else:
                project_id = str(uuid.uuid4())
                bind.execute(
                    sa.text(
                        "INSERT INTO projects "
                        "(id, organization_id, user_id, workspace_id, name, preset_id, config, "
                        "created_at, updated_at) "
                        "VALUES (:id, :org_id, :user_id, :wid, :name, NULL, '{}', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": project_id,
                        "org_id": org_id,
                        "user_id": owner_id,
                        "wid": workspace_id,
                        "name": workspace_name,
                    },
                )
            bind.execute(
                sa.text(
                    "UPDATE sessions SET project_id = :pid "
                    "WHERE workspace_id = :wid AND project_id IS NULL"
                ),
                {"pid": project_id, "wid": workspace_id},
            )


def _default_username(bind, org_id: str) -> str:
    """Return a globally unique username for an organization's default user.

    ``username`` is deployment-unique, so when ``default`` is already taken by
    another organization the backfill must fall back to a deterministic
    org-scoped name instead of failing the whole migration.
    """

    candidate = "default"
    while bind.execute(
        sa.text("SELECT 1 FROM users WHERE username = :name"),
        {"name": candidate},
    ).fetchone():
        candidate = f"default-{org_id[:8]}"
        if bind.execute(
            sa.text("SELECT 1 FROM users WHERE username = :name"),
            {"name": candidate},
        ).fetchone():
            candidate = f"default-{org_id[:8]}-{uuid.uuid4().hex[:6]}"
            break
    return candidate
