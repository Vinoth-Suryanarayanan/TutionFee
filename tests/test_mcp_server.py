"""
tests/test_mcp_server.py
------------------------
Tests for src/tuition/mcp_server.py.

What we're testing:
  _parse_billing_period():
    1. Valid ISO date returns first-of-month date
    2. Day component normalised to 1 (e.g. "2026-01-15" → 2026-01-01)
    3. Invalid string raises ValueError

  run_billing_tool():
    4. Happy path — charges_created > 0, correct structure
    5. Idempotent — second call returns charges_created = 0
    6. Bad date returns error dict (ValueError)

  import_paypal_csv_tool():
    7. Happy path — inserted count matches rows in CSV
    8. Missing file returns error in errors list
    9. Idempotent — second call returns inserted = 0

  import_bank_csv_tool():
    10. Happy path — inserts credit rows
    11. Missing file returns error

  reconcile_all_pending_tool():
    12. Happy path — matched count matches reconcilable transactions
    13. Idempotent — second call, all skipped
    14. Source filter — paypal only, bank transaction not processed
    15. Bad source returns error key

Strategy
--------
We patch `tuition.tools.mcp_server.get_session` to return a context-managed
in-memory SQLite session, so the tools never touch dev.db and tests
are fully isolated.  We also use tmp_path for CSV files.
"""

from __future__ import annotations

import contextlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tuition.db.models import (
    Base,
    Enrollment,
    Family,
    Guardian,
    Student,
    TransactionSource,
)
from tuition.tools.mcp_server import (
    _parse_billing_period,
    import_bank_csv_tool,
    import_paypal_csv_tool,
    reconcile_all_pending_tool,
    run_billing_tool,
)


# ── In-memory session factory ─────────────────────────────────────────────────

def make_engine():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    return engine


@contextlib.contextmanager
def _make_session(engine):
    """Yield a Session that auto-commits on exit (mimics get_session behaviour)."""
    with Session(engine) as session:
        yield session


def patch_get_session(engine):
    """Return a context manager that patches get_session for one engine."""
    return patch(
        "tuition.tools.mcp_server.get_session",
        side_effect=lambda: _make_session(engine),
    )


# ── Seed helpers ──────────────────────────────────────────────────────────────

BILLING_JAN = "2026-01-01"


def _seed_family_with_enrollment(session, *, program="Carnatic", fee="40.00"):
    """Create a minimal family + student + enrollment.  Returns (family, enrollment)."""
    family = Family(external_key="FAM001", display_name="Smith Family")
    session.add(family)
    session.flush()

    session.add(Guardian(
        family_id=family.id,
        full_name="Robert Smith",
        email="robert@test.com",
        is_primary=True,
        preferred_channel="email",
    ))
    session.flush()

    student = Student(
        family_id=family.id,
        external_key="STU001",
        first_name="Alice",
        last_name="Smith",
    )
    session.add(student)
    session.flush()

    enrollment = Enrollment(
        student_id=student.id,
        program=program,
        monthly_fee=Decimal(fee),
        start_date=date(2025, 9, 1),
    )
    session.add(enrollment)
    session.flush()
    return family, enrollment


# ── CSV helpers ───────────────────────────────────────────────────────────────

PAYPAL_HEADER = (
    '"Date","Time","TimeZone","Name","Type","Status","Currency","Gross","Fee","Net",'
    '"From Email Address","To Email Address","Transaction ID","Shipping Address",'
    '"Address Status","Item Title","Item ID","Shipping and Handling Amount",'
    '"Insurance Amount","Sales Tax","Option 1 Name","Option 1 Value",'
    '"Option 2 Name","Option 2 Value","Reference Txn ID","Invoice Number",'
    '"Custom Number","Quantity","Receipt ID","Balance","Subject","Note"'
)


def _paypal_row(*, txn_id="PP-MCP001", name="Robert Smith", gross="40.00"):
    cols = [
        "01/05/2026", "09:00:00", "PST", name,
        "General Payment", "Completed", "USD",
        gross, "0.00", gross,
        "parent@gmail.com", "vrukshaa@gmail.com", txn_id,
        "", "", "", "", "", "", "", "", "", "", "",
        "", "", "", "1", "", "", "Tuition", "Jan",
    ]
    return ",".join(f'"{c}"' for c in cols)


def _write_paypal_csv(tmp_path, rows, *, filename="pp.csv") -> Path:
    p = tmp_path / filename
    p.write_text("\n".join([PAYPAL_HEADER] + rows), encoding="utf-8")
    return p


BANK_HEADER = "Date,Description,Amount,Reference,Type"


def _bank_row(*, ref="BNK-MCP001", name="SMITH ROBERT", amount="40.00"):
    return f"2026-01-06,{name},{amount},{ref},Credit"


def _write_bank_csv(tmp_path, rows, *, filename="bank.csv") -> Path:
    p = tmp_path / filename
    p.write_text("\n".join([BANK_HEADER] + rows), encoding="utf-8")
    return p


# ── Tests: _parse_billing_period ──────────────────────────────────────────────

class TestParseBillingPeriod:
    def test_valid_date_returns_date(self):
        assert _parse_billing_period("2026-01-01") == date(2026, 1, 1)

    def test_day_normalised_to_first(self):
        # "2026-01-15" should become 2026-01-01
        result = _parse_billing_period("2026-01-15")
        assert result == date(2026, 1, 1)

    def test_invalid_string_raises_value_error(self):
        with pytest.raises(ValueError, match="billing_period must be"):
            _parse_billing_period("January 2026")


# ── Tests: run_billing_tool ───────────────────────────────────────────────────

class TestRunBillingTool:
    def test_happy_path_returns_charges(self, tmp_path):
        engine = make_engine()
        with Session(engine) as session:
            _seed_family_with_enrollment(session)
            session.commit()

        with patch_get_session(engine):
            result = run_billing_tool(BILLING_JAN)

        assert result["charges_created"] == 1
        assert result["period"] == BILLING_JAN
        assert len(result["charges"]) == 1
        charge = result["charges"][0]
        assert charge["amount_due"] == "40.00"
        assert charge["status"] == "pending"

    def test_idempotent_second_call_zero_new(self):
        engine = make_engine()
        with Session(engine) as session:
            _seed_family_with_enrollment(session)
            session.commit()

        with patch_get_session(engine):
            first = run_billing_tool(BILLING_JAN)
            second = run_billing_tool(BILLING_JAN)

        assert first["charges_created"] == 1
        assert second["charges_created"] == 0

    def test_bad_date_raises_value_error(self):
        with pytest.raises(ValueError):
            run_billing_tool("not-a-date")


# ── Tests: import_paypal_csv_tool ─────────────────────────────────────────────

class TestImportPaypalCsvTool:
    def test_happy_path_inserts_row(self, tmp_path):
        engine = make_engine()
        csv_path = _write_paypal_csv(tmp_path, [_paypal_row()])

        with patch_get_session(engine):
            result = import_paypal_csv_tool(str(csv_path))

        assert result["inserted"] == 1
        assert result["errors"] == []

    def test_missing_file_returns_error(self):
        engine = make_engine()

        with patch_get_session(engine):
            result = import_paypal_csv_tool("/nonexistent/path/pp.csv")

        assert result["inserted"] == 0
        assert any("not found" in e for e in result["errors"])

    def test_idempotent_second_import(self, tmp_path):
        engine = make_engine()
        csv_path = _write_paypal_csv(tmp_path, [_paypal_row(txn_id="PP-IDEM01")])

        with patch_get_session(engine):
            first = import_paypal_csv_tool(str(csv_path))
            second = import_paypal_csv_tool(str(csv_path))

        assert first["inserted"] == 1
        assert second["inserted"] == 0
        assert second["skipped"] == 1

    def test_recipient_email_filter(self, tmp_path):
        engine = make_engine()
        rows = [
            _paypal_row(txn_id="PP-F01"),
            _paypal_row(txn_id="PP-F02"),
        ]
        csv_path = _write_paypal_csv(tmp_path, rows)

        with patch_get_session(engine):
            result = import_paypal_csv_tool(str(csv_path), recipient_email="vrukshaa@gmail.com")

        assert result["inserted"] == 2


# ── Tests: import_bank_csv_tool ───────────────────────────────────────────────

class TestImportBankCsvTool:
    def test_happy_path_inserts_credit(self, tmp_path):
        engine = make_engine()
        csv_path = _write_bank_csv(tmp_path, [_bank_row()])

        with patch_get_session(engine):
            result = import_bank_csv_tool(str(csv_path))

        assert result["inserted"] == 1
        assert result["errors"] == []

    def test_missing_file_returns_error(self):
        engine = make_engine()

        with patch_get_session(engine):
            result = import_bank_csv_tool("/nonexistent/bank.csv")

        assert result["inserted"] == 0
        assert any("not found" in e for e in result["errors"])


# ── Tests: reconcile_all_pending_tool ─────────────────────────────────────────

class TestReconcileAllPendingTool:
    def _setup(self, engine, tmp_path):
        """Seed DB + import a PayPal row and run billing."""
        with Session(engine) as session:
            _seed_family_with_enrollment(session)
            session.commit()

        with patch_get_session(engine):
            run_billing_tool(BILLING_JAN)

        csv_path = _write_paypal_csv(tmp_path, [_paypal_row()])
        with patch_get_session(engine):
            import_paypal_csv_tool(str(csv_path))

    def test_happy_path_matched(self, tmp_path):
        engine = make_engine()
        self._setup(engine, tmp_path)

        with patch_get_session(engine):
            result = reconcile_all_pending_tool(BILLING_JAN)

        assert result["total"] == 1
        assert result["matched"] == 1
        assert result["period"] == BILLING_JAN
        assert len(result["results"]) == 1
        assert result["results"][0]["outcome"] == "matched"

    def test_idempotent_second_call_all_skipped(self, tmp_path):
        engine = make_engine()
        self._setup(engine, tmp_path)

        with patch_get_session(engine):
            first = reconcile_all_pending_tool(BILLING_JAN)
            second = reconcile_all_pending_tool(BILLING_JAN)

        assert first["matched"] == 1
        assert second["skipped"] == 1

    def test_source_filter_paypal_only(self, tmp_path):
        """With source='paypal', only PayPal transactions are processed."""
        engine = make_engine()

        # Seed family + two enrollments (so we have charges for both txns)
        with Session(engine) as session:
            family = Family(external_key="FAM001", display_name="Smith Family")
            session.add(family)
            session.flush()
            session.add(Guardian(
                family_id=family.id,
                full_name="Robert Smith",
                email="robert@test.com",
                is_primary=True,
                preferred_channel="email",
            ))
            student = Student(
                family_id=family.id, external_key="STU001",
                first_name="Alice", last_name="Smith",
            )
            session.add(student)
            session.flush()
            session.add(Enrollment(
                student_id=student.id, program="Carnatic",
                monthly_fee=Decimal("40.00"), start_date=date(2025, 9, 1),
            ))
            session.add(Enrollment(
                student_id=student.id, program="Yoga",
                monthly_fee=Decimal("40.00"), start_date=date(2025, 9, 1),
            ))
            session.commit()

        with patch_get_session(engine):
            run_billing_tool(BILLING_JAN)

        pp_csv = _write_paypal_csv(tmp_path, [_paypal_row(txn_id="PP-SRC01")], filename="pp.csv")
        bank_csv = _write_bank_csv(tmp_path, [_bank_row(ref="BNK-SRC01")], filename="bank.csv")

        with patch_get_session(engine):
            import_paypal_csv_tool(str(pp_csv))
            import_bank_csv_tool(str(bank_csv))

        with patch_get_session(engine):
            result = reconcile_all_pending_tool(BILLING_JAN, source="paypal")

        # Only the PayPal transaction was reconciled
        assert result["total"] == 1
        assert result["results"][0]["external_id"] == "PP-SRC01"

    def test_bad_source_returns_error_key(self):
        engine = make_engine()

        with patch_get_session(engine):
            result = reconcile_all_pending_tool(BILLING_JAN, source="venmo")

        assert "error" in result
        assert "venmo" in result["error"]
