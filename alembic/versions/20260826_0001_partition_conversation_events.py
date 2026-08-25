"""Partition conversation_events by month (PostgreSQL).

The event table grows without bound for long-lived conversations. Monthly
RANGE partitioning on ``occurred_at`` gives:

- partition pruning for time-window queries and the retention cleanup;
- cheap archival: whole idle month partitions can be dropped instead of
  row-by-row DELETEs.

The application models keep the plain-table definition so development /
SQLite paths are unchanged; this migration converts an existing PostgreSQL
table in place (copy -> swap). Writes are protected by the repository's
partition-ensure helper, so future months get a partition automatically.

Revision ID: 20260826_0001
Revises: 20260825_0001
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from alembic import op

revision: str = "20260826_0001"
down_revision: str | None = "20260825_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _month_range(month_start: datetime) -> tuple[datetime, datetime]:
    start = month_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def _create_partition(month: str, start: datetime, end: datetime) -> None:
    partition = f"conversation_events_{month}"
    op.execute(
        f"CREATE TABLE IF NOT EXISTS {partition} PARTITION OF "
        f"conversation_events_partitioned FOR VALUES FROM ('{start.isoformat()}') "
        f"TO ('{end.isoformat()}')"
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{partition}_conversation_id_seq "
        f"ON {partition} (conversation_id, seq)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{partition}_conversation_id "
        f"ON {partition} (conversation_id)"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    relkind = bind.execute(
        sa.text("SELECT relkind FROM pg_class WHERE relname = 'conversation_events'")
    ).scalar_one_or_none()
    if relkind == "p":  # already partitioned
        return
    if relkind is None:
        raise RuntimeError("conversation_events table does not exist")

    op.execute(
        "CREATE TABLE conversation_events_partitioned "
        "(LIKE conversation_events INCLUDING DEFAULTS) "
        "PARTITION BY RANGE (occurred_at)"
    )

    # Partitions for every month that already has data.
    months = [
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT to_char(date_trunc('month', occurred_at), 'YYYYMM') "
                "FROM conversation_events"
            )
        ).fetchall()
    ]
    for month in months:
        start, end = _month_range(
            datetime.strptime(month, "%Y%m").replace(tzinfo=UTC)
        )
        _create_partition(month, start, end)

    # A few future partitions so writes continue after the migration.
    now = datetime.now(UTC)
    for offset in range(4):
        month_start = (now.replace(day=1) + timedelta(days=32 * offset)).replace(
            day=1
        )
        start, end = _month_range(month_start)
        _create_partition(start.strftime("%Y%m"), start, end)

    op.execute(
        "INSERT INTO conversation_events_partitioned "
        "SELECT * FROM conversation_events"
    )
    op.execute("DROP TABLE conversation_events")
    op.execute("ALTER TABLE conversation_events_partitioned RENAME TO conversation_events")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    relkind = bind.execute(
        sa.text("SELECT relkind FROM pg_class WHERE relname = 'conversation_events'")
    ).scalar_one_or_none()
    if relkind != "p":
        return
    op.execute(
        "CREATE TABLE conversation_events_legacy "
        "(LIKE conversation_events INCLUDING ALL)"
    )
    op.execute(
        "INSERT INTO conversation_events_legacy SELECT * FROM conversation_events"
    )
    op.execute("DROP TABLE conversation_events")
    op.execute("ALTER TABLE conversation_events_legacy RENAME TO conversation_events")
