"""create_agent_tables

Step 2c: agent_runs, llm_calls, reminders.

Revision ID: 9c1e83fa5d22
Revises: 7b3f12de4a01
Create Date: 2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c1e83fa5d22"
down_revision: str | None = "7b3f12de4a01"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # -- agent_runs ------------------------------------------------------------
    # One row per agent execution.  All LLM calls and reminders link back here.
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_name", sa.String(80), nullable=False),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column(
            "status",
            sa.Enum("running", "succeeded", "failed", "cancelled",
                    name="agentstatus", native_enum=False, length=32),
            nullable=False,
            server_default="running",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
    )
    op.create_index("ix_agent_runs_agent_name", "agent_runs", ["agent_name"])
    op.create_index("ix_agent_runs_started_at", "agent_runs", ["started_at"])

    # -- llm_calls -------------------------------------------------------------
    # One row per Claude API call -- the raw data for the model-cost dashboard.
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_run_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("purpose", sa.String(120), nullable=True),
        # Separate input/output because they have different per-token prices.
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        # Float is fine for cost analytics -- we're not billing anyone from this.
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_llm_calls"),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.id"],
            name="fk_llm_calls_agent_run_id_agent_runs",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("input_tokens >= 0", name="ck_llm_calls_llm_input_tokens_non_negative"),
        sa.CheckConstraint("output_tokens >= 0",
                           name="ck_llm_calls_llm_output_tokens_non_negative"),
        sa.CheckConstraint("cost_usd >= 0", name="ck_llm_calls_llm_cost_non_negative"),
    )
    op.create_index("ix_llm_calls_agent_run_id", "llm_calls", ["agent_run_id"])
    op.create_index("ix_llm_calls_called_at", "llm_calls", ["called_at"])

    # -- reminders -------------------------------------------------------------
    # One row per message sent (or queued) to a parent.
    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_run_id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        # NULL = sent to whole family rather than a specific guardian.
        sa.Column("guardian_id", sa.Integer(), nullable=True),
        sa.Column(
            "channel",
            sa.Enum("email", "sms", name="channel", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        # JSON list of charge IDs this reminder covered, e.g. "[12, 13]".
        # Stored as Text so it works identically on SQLite and Postgres.
        sa.Column("charge_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "status",
            sa.Enum("pending", "sent", "delivered", "failed",
                    name="reminderstatus", native_enum=False, length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_reminders"),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.id"],
            name="fk_reminders_agent_run_id_agent_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"],
            name="fk_reminders_family_id_families",
            ondelete="CASCADE",
        ),
        # SET NULL: reminder history survives even if a guardian record is removed.
        sa.ForeignKeyConstraint(
            ["guardian_id"], ["guardians.id"],
            name="fk_reminders_guardian_id_guardians",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_reminders_family_id", "reminders", ["family_id"])
    op.create_index("ix_reminders_sent_at", "reminders", ["sent_at"])


def downgrade() -> None:
    op.drop_index("ix_reminders_sent_at", table_name="reminders")
    op.drop_index("ix_reminders_family_id", table_name="reminders")
    op.drop_table("reminders")

    op.drop_index("ix_llm_calls_called_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_agent_run_id", table_name="llm_calls")
    op.drop_table("llm_calls")

    op.drop_index("ix_agent_runs_started_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_agent_name", table_name="agent_runs")
    op.drop_table("agent_runs")
