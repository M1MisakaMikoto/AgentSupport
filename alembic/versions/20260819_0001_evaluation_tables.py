"""Add evaluation layer tables (ADR-004, phase 1).

Datasets / cases / runs / run-case results persist the evaluation closed
loop so reports and comparisons survive restarts.

Revision ID: 20260819_0001
Revises: 20260817_0001
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260819_0001"
down_revision: str | None = "20260817_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.Column:
    return sa.Column("id", sa.String(36), primary_key=True)


def upgrade() -> None:
    op.create_table(
        "eval_datasets",
        _uuid(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("baseline_version", sa.String(64), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_eval_datasets_workspace_id", "eval_datasets", ["workspace_id"])

    op.create_table(
        "eval_dataset_cases",
        _uuid(),
        sa.Column("dataset_id", sa.String(36), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("verifiers", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_eval_dataset_cases_dataset_id", "eval_dataset_cases", ["dataset_id"])

    op.create_table(
        "eval_runs",
        _uuid(),
        sa.Column("dataset_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("runner_fingerprint", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_eval_runs_dataset_id", "eval_runs", ["dataset_id"])

    op.create_table(
        "eval_run_cases",
        _uuid(),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("case_id", sa.String(36), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("verifier_results", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("cost_estimate", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_eval_run_cases_run_id", "eval_run_cases", ["run_id"])
    op.create_index("ix_eval_run_cases_case_id", "eval_run_cases", ["case_id"])
    op.create_check_constraint(
        "ck_eval_runs_status",
        "eval_runs",
        "status IN ('pending','running','completed','failed')",
    )
    op.create_check_constraint(
        "ck_eval_run_cases_verdict",
        "eval_run_cases",
        "verdict IN ('PASS','FAIL','ERROR','UNCERTAIN')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_eval_run_cases_verdict", "eval_run_cases", type_="check")
    op.drop_constraint("ck_eval_runs_status", "eval_runs", type_="check")
    op.drop_index("ix_eval_run_cases_case_id", table_name="eval_run_cases")
    op.drop_index("ix_eval_run_cases_run_id", table_name="eval_run_cases")
    op.drop_table("eval_run_cases")
    op.drop_index("ix_eval_runs_dataset_id", table_name="eval_runs")
    op.drop_table("eval_runs")
    op.drop_index("ix_eval_dataset_cases_dataset_id", table_name="eval_dataset_cases")
    op.drop_table("eval_dataset_cases")
    op.drop_index("ix_eval_datasets_workspace_id", table_name="eval_datasets")
    op.drop_table("eval_datasets")
