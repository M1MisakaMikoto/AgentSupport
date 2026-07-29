"""Use event identity instead of Runner-local sequence for deduplication.

Revision ID: 20260728_0003
Revises: 20260728_0002
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260728_0003"
down_revision: str | None = "20260728_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    for constraint in sa.inspect(bind).get_unique_constraints("conversation_events"):
        if set(constraint.get("column_names") or []) == {"run_id", "runner_seq"}:
            if bind.dialect.name == "sqlite":
                naming_convention = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
                with op.batch_alter_table(
                    "conversation_events", naming_convention=naming_convention
                ) as batch:
                    batch.drop_constraint(
                        constraint["name"] or "uq_conversation_events_run_id",
                        type_="unique",
                    )
            else:
                op.drop_constraint(
                    constraint["name"], "conversation_events", type_="unique"
                )


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_conversation_events_run_runner_seq",
        "conversation_events",
        ["run_id", "runner_seq"],
    )
