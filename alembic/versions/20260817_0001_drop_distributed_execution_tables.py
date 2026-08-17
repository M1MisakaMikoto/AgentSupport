"""Drop the retired distributed execution tables.

The Temporal execution backend replaced ExecutionJob / RunCommand /
RunnerEndpoint / RuntimeSlot coordination (ADR-003, phase 2). Conversation
checkpoints are kept: the inline pause path and the Temporal segment
checkpoint contract both use the table.

Revision ID: 20260817_0001
Revises: 20260814_0001
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260817_0001"
down_revision: str | None = "20260814_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROPPED_TABLES = [
    "execution_jobs",
    "run_commands",
    "runner_endpoints",
    "runtime_slots",
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    for table in _DROPPED_TABLES:
        if table not in existing:
            continue
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table) as batch:
                batch.drop_table()
        else:
            op.drop_table(table)


def downgrade() -> None:
    # The distributed execution layer is retired; tables are not recreated.
    pass
