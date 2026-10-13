"""
tests/test_importer.py
----------------------
Tests for src/tuition/importer.py — import_paypal_csv() and import_bank_csv().

What we're testing:
  PayPal importer:
    1.  Happy path: valid CSV inserts all rows
    2.  Idempotency: re-importing same file inserts 0, skips all
    3.  CSV-level duplicate rows (same Transaction ID twice) → counted separately
    4.  Recipient email filter: only rows matching target email are imported
    5.  Non-positive amounts (negative gross) are skipped with an error
    6.  Empty Transaction ID rows are skipped with an error
    7.  Bad date format produces an error entry, rest of file continues
    8.  payer_name and memo are captured correctly
    9.  Returns correct ImportResult counts

  Bank importer:
    10. Happy path: valid CSV inserts credit rows
    11. Debit rows are silently ignored
    12. Idempotency: re-importing skips all
    13. Empty Reference is skipped with an error
    14. Bad date produces an error, rest continues
    15. Description maps to both payer_name and memo

Strategy: we write CSV content to temporary files using tmp_path (pytest's
built-in temp directory fixture) so no permanent files are created.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tuition.db.models import Base, Transaction, TransactionSource
from tuition.core.importer import ImportResult, import_bank_csv, import_paypal_csv


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_session():
    """Fresh in-memory SQLite session for each test."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


# ── PayPal CSV builders ───────────────────────────────────────────────────────

PAYPAL_HEADER = (
    '"Date","Time","TimeZone","Name","Type","Status","Currency","Gross","Fee","Net",'
    '"From Email Address","To Email Address","Transaction ID","Shipping Address",'
    '"Address Status","Item Title","Item ID","Shipping and Handling Amount",'
    '"Insurance Amount","Sales Tax","Option 1 Name","Option 1 Value",'
    '"Option 2 Name","Option 2 Value","Reference Txn ID","Invoice Number",'
    '"Custom Number","Quantity","Receipt ID","Balance","Subject","Note"'
)


def paypal_row(
    *,
    date_str: str = "01/05/2026",
    name: str = "Robert Smith",
    gross: str = "40.00",
    txn_id: str = "PP-TEST001",
    to_email: str = "vrukshaa@gmail.com",
    subject: str = "Jan tuition",
    note: str = "Carnatic",
) -> str:
    """Build one data row in PayPal CSV format (32 columns)."""
    cols = [
        date_str, "09:00:00", "PST", name,
        "General Payment", "Completed", "USD",
        gross, "0.00", gross,
        "parent@gmail.com", to_email, txn_id,
        "", "", "", "", "", "", "", "", "", "", "",
        "", "", "", "1", "", "", subject, note,
    ]
    return ",".join(f'"{c}"' for c in cols)


def write_paypal_csv(tmp_path: Path, rows: list[str], *, filename: str = "pp.csv") -> Path:
    """Write a complete PayPal CSV to a temp file and return its path."""
    path = tmp_path / filename
    path.write_text("\n".join([PAYPAL_HEADER] + rows), encoding="utf-8")
    return path


# ── Bank CSV builders ─────────────────────────────────────────────────────────

BANK_HEADER = "Date,Description,Amount,Reference,Type"


def bank_row(
    *,
    date_str: str = "2026-01-06",
    description: str = "SMITH ROBERT VRUKSHAA",
    amount: str = "40.00",
    ref: str = "BNK-TEST001",
    txn_type: str = "Credit",
) -> str:
    return f"{date_str},{description},{amount},{ref},{txn_type}"


def write_bank_csv(tmp_path: Path, rows: list[str], *, filename: str = "bank.csv") -> Path:
    path = tmp_path / filename
    path.write_text("\n".join([BANK_HEADER] + rows), encoding="utf-8")
    return path


# ── PayPal tests ──────────────────────────────────────────────────────────────

class TestPaypalHappyPath:
    def test_inserts_single_row(self, db_session, tmp_path):
        csv_path = write_paypal_csv(tmp_path, [paypal_row()])
        result = import_paypal_csv(db_session, csv_path)

        assert result.inserted == 1
        assert result.skipped == 0
        assert result.errors == []

    def test_inserts_multiple_rows(self, db_session, tmp_path):
        rows = [
            paypal_row(txn_id="PP-001", name="Alice"),
            paypal_row(txn_id="PP-002", name="Bob", gross="80.00"),
            paypal_row(txn_id="PP-003", name="Carol"),
        ]
        csv_path = write_paypal_csv(tmp_path, rows)
        result = import_paypal_csv(db_session, csv_path)

        assert result.inserted == 3
        assert result.errors == []

    def test_correct_field_values_stored(self, db_session, tmp_path):
        csv_path = write_paypal_csv(
            tmp_path,
            [paypal_row(
                date_str="01/15/2026",
                name="Test Parent",
                gross="40.00",
                txn_id="PP-FIELD01",
                subject="Jan tuition",
                note="Carnatic Jan",
            )],
        )
        import_paypal_csv(db_session, csv_path)

        txn = db_session.scalars(
            select(Transaction).filter_by(external_id="PP-FIELD01")
        ).first()
        assert txn is not None
        assert txn.source == TransactionSource.PAYPAL
        assert txn.paid_on == date(2026, 1, 15)
        assert txn.amount == Decimal("40.00")
        assert txn.payer_name == "Test Parent"
        assert "Jan tuition" in txn.memo
        assert "Carnatic Jan" in txn.memo

    def test_empty_csv_returns_zero(self, db_session, tmp_path):
        csv_path = write_paypal_csv(tmp_path, [])
        result = import_paypal_csv(db_session, csv_path)
        assert result.inserted == 0


class TestPaypalIdempotency:
    def test_second_import_skips_all(self, db_session, tmp_path):
        csv_path = write_paypal_csv(tmp_path, [paypal_row(txn_id="PP-IDEM01")])

        first = import_paypal_csv(db_session, csv_path)
        db_session.flush()
        second = import_paypal_csv(db_session, csv_path)

        assert first.inserted == 1
        assert second.inserted == 0
        assert second.skipped == 1

    def test_db_has_only_one_row_after_two_imports(self, db_session, tmp_path):
        csv_path = write_paypal_csv(tmp_path, [paypal_row(txn_id="PP-IDEM02")])

        import_paypal_csv(db_session, csv_path)
        db_session.flush()
        import_paypal_csv(db_session, csv_path)

        count = len(db_session.scalars(select(Transaction)).all())
        assert count == 1


class TestPaypalDuplicatesInCSV:
    def test_same_txn_id_twice_counted_as_csv_duplicate(self, db_session, tmp_path):
        """PayPal sometimes exports the same transaction twice in one file."""
        row = paypal_row(txn_id="PP-DUP01")
        csv_path = write_paypal_csv(tmp_path, [row, row])  # same row twice

        result = import_paypal_csv(db_session, csv_path)

        assert result.inserted == 1
        assert result.duplicate_in_csv == 1

    def test_three_identical_rows_counted_correctly(self, db_session, tmp_path):
        row = paypal_row(txn_id="PP-DUP02")
        csv_path = write_paypal_csv(tmp_path, [row, row, row])

        result = import_paypal_csv(db_session, csv_path)

        assert result.inserted == 1
        assert result.duplicate_in_csv == 2


class TestPaypalEmailFilter:
    def test_rows_matching_recipient_are_imported(self, db_session, tmp_path):
        rows = [
            paypal_row(txn_id="PP-FILT01", to_email="vrukshaa@gmail.com"),
            paypal_row(txn_id="PP-FILT02", to_email="vrukshaa@gmail.com"),
        ]
        csv_path = write_paypal_csv(tmp_path, rows)
        result = import_paypal_csv(db_session, csv_path, recipient_email="vrukshaa@gmail.com")

        assert result.inserted == 2

    def test_rows_not_matching_recipient_are_skipped(self, db_session, tmp_path):
        rows = [
            paypal_row(txn_id="PP-FILT03", to_email="other@example.com"),
            paypal_row(txn_id="PP-FILT04", to_email="vrukshaa@gmail.com"),
        ]
        csv_path = write_paypal_csv(tmp_path, rows)
        result = import_paypal_csv(db_session, csv_path, recipient_email="vrukshaa@gmail.com")

        assert result.inserted == 1  # only PP-FILT04
        assert result.skipped == 1   # PP-FILT03 skipped

    def test_no_filter_imports_all(self, db_session, tmp_path):
        rows = [
            paypal_row(txn_id="PP-FILT05", to_email="a@example.com"),
            paypal_row(txn_id="PP-FILT06", to_email="b@example.com"),
        ]
        csv_path = write_paypal_csv(tmp_path, rows)
        result = import_paypal_csv(db_session, csv_path)  # no filter

        assert result.inserted == 2


class TestPaypalErrorHandling:
    def test_empty_txn_id_produces_error(self, db_session, tmp_path):
        row = paypal_row(txn_id="")  # empty ID
        csv_path = write_paypal_csv(tmp_path, [row])
        result = import_paypal_csv(db_session, csv_path)

        assert result.inserted == 0
        assert len(result.errors) == 1

    def test_bad_date_produces_error_but_continues(self, db_session, tmp_path):
        rows = [
            paypal_row(txn_id="PP-GOOD01"),
            paypal_row(txn_id="PP-BAD01", date_str="not-a-date"),
            paypal_row(txn_id="PP-GOOD02"),
        ]
        csv_path = write_paypal_csv(tmp_path, rows)
        result = import_paypal_csv(db_session, csv_path)

        assert result.inserted == 2      # two good rows
        assert len(result.errors) == 1   # one bad date

    def test_zero_amount_produces_error(self, db_session, tmp_path):
        row = paypal_row(txn_id="PP-ZERO01", gross="0.00")
        csv_path = write_paypal_csv(tmp_path, [row])
        result = import_paypal_csv(db_session, csv_path)

        assert result.inserted == 0
        assert len(result.errors) >= 1


# ── Bank tests ────────────────────────────────────────────────────────────────

class TestBankHappyPath:
    def test_inserts_credit_row(self, db_session, tmp_path):
        csv_path = write_bank_csv(tmp_path, [bank_row()])
        result = import_bank_csv(db_session, csv_path)

        assert result.inserted == 1
        assert result.errors == []

    def test_correct_field_values_stored(self, db_session, tmp_path):
        csv_path = write_bank_csv(
            tmp_path,
            [bank_row(
                date_str="2026-01-15",
                description="GARCIA MIGUEL VRUKSHAA",
                amount="10.00",
                ref="BNK-FIELD01",
            )],
        )
        import_bank_csv(db_session, csv_path)

        txn = db_session.scalars(
            select(Transaction).filter_by(external_id="BNK-FIELD01")
        ).first()
        assert txn is not None
        assert txn.source == TransactionSource.BANK
        assert txn.paid_on == date(2026, 1, 15)
        assert txn.amount == Decimal("10.00")
        assert txn.payer_name == "GARCIA MIGUEL VRUKSHAA"
        assert txn.memo == "GARCIA MIGUEL VRUKSHAA"

    def test_inserts_multiple_credits(self, db_session, tmp_path):
        rows = [
            bank_row(ref="BNK-M01"),
            bank_row(ref="BNK-M02", amount="80.00"),
            bank_row(ref="BNK-M03", description="ANOTHER FAMILY"),
        ]
        csv_path = write_bank_csv(tmp_path, rows)
        result = import_bank_csv(db_session, csv_path)

        assert result.inserted == 3


class TestBankDebits:
    def test_debit_rows_are_ignored(self, db_session, tmp_path):
        rows = [
            bank_row(ref="BNK-CR01", txn_type="Credit"),
            bank_row(ref="BNK-DB01", txn_type="Debit"),  # e.g. bank fee
        ]
        csv_path = write_bank_csv(tmp_path, rows)
        result = import_bank_csv(db_session, csv_path)

        assert result.inserted == 1  # only the Credit

    def test_only_debits_file_inserts_nothing(self, db_session, tmp_path):
        rows = [bank_row(ref="BNK-DB02", txn_type="Debit")]
        csv_path = write_bank_csv(tmp_path, rows)
        result = import_bank_csv(db_session, csv_path)

        assert result.inserted == 0


class TestBankIdempotency:
    def test_second_import_skips_all(self, db_session, tmp_path):
        csv_path = write_bank_csv(tmp_path, [bank_row(ref="BNK-IDEM01")])

        first = import_bank_csv(db_session, csv_path)
        db_session.flush()
        second = import_bank_csv(db_session, csv_path)

        assert first.inserted == 1
        assert second.inserted == 0
        assert second.skipped == 1


class TestBankErrorHandling:
    def test_empty_reference_produces_error(self, db_session, tmp_path):
        csv_path = write_bank_csv(tmp_path, [bank_row(ref="")])
        result = import_bank_csv(db_session, csv_path)

        assert result.inserted == 0
        assert len(result.errors) == 1

    def test_bad_date_produces_error_but_continues(self, db_session, tmp_path):
        rows = [
            bank_row(ref="BNK-GOOD01"),
            bank_row(ref="BNK-BAD01", date_str="not-a-date"),
            bank_row(ref="BNK-GOOD02"),
        ]
        csv_path = write_bank_csv(tmp_path, rows)
        result = import_bank_csv(db_session, csv_path)

        assert result.inserted == 2
        assert len(result.errors) == 1


class TestImportResultCounts:
    def test_total_rows_seen_sums_correctly(self, db_session, tmp_path):
        """total_rows_seen = inserted + skipped + duplicate_in_csv + errors"""
        rows = [
            paypal_row(txn_id="PP-S01"),          # will insert
            paypal_row(txn_id="PP-S01"),           # CSV duplicate
            paypal_row(txn_id="PP-S02", date_str="bad"),  # error
        ]
        csv_path = write_paypal_csv(tmp_path, rows)
        result = import_paypal_csv(db_session, csv_path)

        assert result.inserted == 1
        assert result.duplicate_in_csv == 1
        assert len(result.errors) == 1
        assert result.total_rows_seen == 3
