"""create_money_tables

Step 2b: charges, transactions, allocations.

Revision ID: 7b3f12de4a01
Revises: 4a9abceb93c7
Create Date: 2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b3f12de4a01"
down_revision: str | None = "4a9abceb93c7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── charges ──────────────────────────────────────────────────────────────
    op.create_table(
        "charges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enrollment_id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("billing_period", sa.Date(), nullable=False),
        sa.Column("amount_due", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "paid", "partial", "waived",
                    name="chargestatus", native_enum=False, length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_charges"),
        sa.ForeignKeyConstraint(
            ["enrollment_id"], ["enrollments.id"],
            name="fk_charges_enrollment_id_enrollments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"],
            name="fk_charges_family_id_families",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("enrollment_id", "billing_period", name="uq_charge_enrollment_period"),
        sa.CheckConstraint("amount_due >= 0", name="ck_charges_charge_amount_non_negative"),
    )

    # ── transactions ─────────────────────────────────────────────────────────
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=True),
        sa.Column(
            "source",
            sa.Enum("paypal", "bank", "manual",
                    name="transactionsource", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("payer_name", sa.String(200), nullable=True),
        sa.Column("memo", sa.String(500), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_transactions"),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"],
            name="fk_transactions_family_id_families",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("source", "external_id", name="uq_transaction_source_external"),
        sa.CheckConstraint("amount > 0", name="ck_transactions_txn_amount_positive"),
    )
    op.create_index("ix_transactions_family_id", "transactions", ["family_id"])

    # ── allocations ───────────────────────────────────────────────────────────
    op.create_table(
        "allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("charge_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("confirmed_by_human", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_allocations"),
        sa.ForeignKeyConstraint(
            ["transaction_id"], ["transactions.id"],
            name="fk_allocations_transaction_id_transactions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["charge_id"], ["charges.id"],
            name="fk_allocations_charge_id_charges",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("amount > 0", name="ck_allocations_alloc_amount_positive"),
    )


def downgrade() -> None:
    op.drop_table("allocations")
    op.drop_index("ix_transactions_family_id", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("charges")
