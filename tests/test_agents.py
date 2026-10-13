"""
tests/test_agents.py
---------------------
Tests for the LangGraph agent nodes.

What we test
------------
We test each node in ISOLATION — no real DB, no real LLM, no real graph.

ingestion_node
  1. Happy path: both CSVs imported, results stored in state
  2. Missing CSV paths: errors appended to state, zeros returned

reconciliation_node
  3. Happy path: billing + reconcile run, pending charges collected
  4. No pending charges when fully reconciled

reminder_node
  5. Happy path: one reminder drafted per pending charge (no real API key)
  6. No pending charges: reminder_results is empty

graph (end-to-end, no interrupt)
  7. Full run: state flows through all nodes, all expected keys present

Strategy
--------
- MCP tool functions are patched directly (they call get_session internally)
- LLM calls are patched at the anthropic.Anthropic class level
- _get_pending_charges and _get_open_families are patched to return canned data
- The graph test uses interrupt_before_reminder=False to run end-to-end
"""

from __future__ import annotations

import contextlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tuition.db.models import Base, Enrollment, Family, Guardian, Student

PERIOD = "2026-01-01"


# ── In-memory DB helpers ───────────────────────────────────────────────────────

def make_engine():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    return engine


@contextlib.contextmanager
def _make_session(engine):
    with Session(engine) as session:
        yield session


def _seed(session):
    family = Family(external_key="FAM001", display_name="Smith Family")
    session.add(family)
    session.flush()
    session.add(Guardian(
        family_id=family.id, full_name="Robert Smith",
        email="robert@test.com", is_primary=True, preferred_channel="email",
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
    session.commit()


# ── CSV helpers ───────────────────────────────────────────────────────────────
# PayPal CSV has exactly 32 columns. We build the row as a list so the column
# count is explicit and verifiable — never an inline string that's easy to miscount.
#
# Index -> column name:
#  0:Date  1:Time  2:TZ  3:Name  4:Type  5:Status  6:Currency
#  7:Gross  8:Fee  9:Net  10:FromEmail  11:ToEmail  12:TxnID
#  13:ShippingAddress  14:AddressStatus  15:ItemTitle  16:ItemID
#  17:S&H  18:Insurance  19:Tax  20:Opt1Name  21:Opt1Val
#  22:Opt2Name  23:Opt2Val  24:RefTxnID  25:InvoiceNum  26:CustomNum
#  27:Quantity  28:ReceiptID  29:Balance  30:Subject  31:Note

PAYPAL_HEADER = (
    '"Date","Time","TimeZone","Name","Type","Status","Currency","Gross","Fee","Net",'
    '"From Email Address","To Email Address","Transaction ID","Shipping Address",'
    '"Address Status","Item Title","Item ID","Shipping and Handling Amount",'
    '"Insurance Amount","Sales Tax","Option 1 Name","Option 1 Value",'
    '"Option 2 Name","Option 2 Value","Reference Txn ID","Invoice Number",'
    '"Custom Number","Quantity","Receipt ID","Balance","Subject","Note"'
)


def _paypal_row(*, txn_id: str = "PP-AGT001", name: str = "Robert Smith", gross: str = "40.00") -> str:
    """Return a single quoted-CSV data row with exactly 32 columns."""
    cols = [
        "01/05/2026", "09:00:00", "PST", name,            # 0-3
        "General Payment", "Completed", "USD",              # 4-6
        gross, "0.00", gross,                               # 7-9  Gross, Fee, Net
        "parent@gmail.com", "vrukshaa@gmail.com", txn_id,  # 10-12
        "", "", "", "", "", "", "", "", "", "", "",         # 13-23  Shipping...Opt2Val
        "", "", "", "1", "", "",                            # 24-29  RefTxnID...Balance
        "Tuition", "Jan",                                   # 30-31  Subject, Note
    ]
    assert len(cols) == 32, f"Row must have 32 columns, got {len(cols)}"
    return ",".join(f'"{c}"' for c in cols)


def _write_paypal_csv(tmp_path: Path, *, filename: str = "pp.csv") -> Path:
    p = tmp_path / filename
    p.write_text(f"{PAYPAL_HEADER}\n{_paypal_row()}", encoding="utf-8")
    return p


def _write_bank_csv(tmp_path: Path) -> Path:
    p = tmp_path / "bank.csv"
    p.write_text(
        "Date,Description,Amount,Reference,Type\n"
        "2026-01-06,SMITH ROBERT,40.00,BNK-AGT001,Credit",
        encoding="utf-8",
    )
    return p


# ── Shared pending charge fixture ──────────────────────────────────────────────

PENDING_CHARGE = {
    "charge_id": 1,
    "family_id": 1,
    "family_name": "Smith Family",
    "enrollment_id": 1,
    "program": "Carnatic",
    "student_name": "Alice Smith",
    "amount_due": "40.00",
    "guardian_email": "robert@test.com",
    "guardian_name": "Robert Smith",
}


# ── Helper: block anthropic import without recursion ──────────────────────────

def _no_anthropic_import():
    """
    Return a context manager that makes ``import anthropic`` raise ImportError
    inside the code under test, while leaving all other imports unaffected.

    WHY THE NAIVE LAMBDA CAUSES RECURSION
    --------------------------------------
    This pattern is broken:

        patch("builtins.__import__", side_effect=lambda name, *a, **kw:
            (_ for _ in ()).throw(ImportError(name))
            if name == "anthropic" else __import__(name, *a, **kw)
        )

    The lambda looks up ``__import__`` at *call time*. By then
    builtins.__import__ has been replaced by the patch itself, so every
    non-anthropic import re-enters the same lambda -> infinite recursion.

    THE FIX
    -------
    Capture the real ``__import__`` BEFORE the patch is applied. The fake
    function calls that captured reference — never the patched version.
    """
    import builtins
    _real_import = builtins.__import__          # captured before the patch

    def _fake_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError(name)
        return _real_import(name, *args, **kwargs)  # uses pre-patch reference

    return patch("builtins.__import__", side_effect=_fake_import)


# ── ingestion_node ─────────────────────────────────────────────────────────────

class TestIngestionNode:
    def test_happy_path(self, tmp_path):
        """Both CSVs imported successfully; counts stored in state."""
        from tuition.agents.ingestion import ingestion_node

        engine = make_engine()
        pp = _write_paypal_csv(tmp_path)
        bk = _write_bank_csv(tmp_path)

        with patch("tuition.tools.mcp_server.get_session",
                   side_effect=lambda: _make_session(engine)):
            result = ingestion_node({
                "billing_period": PERIOD,
                "paypal_csv_path": str(pp),
                "bank_csv_path": str(bk),
                "errors": [],
                "total_llm_cost_usd": 0.0,
            })

        assert result["ingestion_result"]["paypal"]["inserted"] == 1
        assert result["ingestion_result"]["bank"]["inserted"] == 1
        assert result["errors"] == []

    def test_missing_csv_paths(self):
        """Empty paths produce errors; inserted counts are zero."""
        from tuition.agents.ingestion import ingestion_node

        engine = make_engine()
        with patch("tuition.tools.mcp_server.get_session",
                   side_effect=lambda: _make_session(engine)):
            result = ingestion_node({
                "billing_period": PERIOD,
                "paypal_csv_path": "",
                "bank_csv_path": "",
                "errors": [],
                "total_llm_cost_usd": 0.0,
            })

        assert result["ingestion_result"]["paypal"]["inserted"] == 0
        assert len(result["errors"]) >= 2


# ── reconciliation_node ────────────────────────────────────────────────────────

class TestReconciliationNode:
    def test_happy_path_pending_charges(self, tmp_path):
        """Unmatched transaction -> LLM assist called -> pending_charges populated."""
        from tuition.agents.reconciliation import reconciliation_node

        engine = make_engine()
        with Session(engine) as session:
            _seed(session)

        # A bank txn whose payer name won't match -> reconciler marks it unmatched
        bk = tmp_path / "bank.csv"
        bk.write_text(
            "Date,Description,Amount,Reference,Type\n"
            "2026-01-06,UNKNOWN PERSON,40.00,BNK-NOMATCH,Credit",
            encoding="utf-8",
        )

        with patch("tuition.tools.mcp_server.get_session",
                   side_effect=lambda: _make_session(engine)), \
             patch("tuition.agents.reconciliation._llm_match") as mock_llm, \
             patch("tuition.agents.reconciliation._get_open_families", return_value=[]), \
             patch("tuition.agents.reconciliation._get_pending_charges",
                   return_value=[PENDING_CHARGE]):

            mock_llm.return_value = {
                "external_id": "BNK-NOMATCH",
                "llm_decision": "unmatched",
                "family_id": None,
                "confidence": "low",
                "notes": "Could not identify family",
                "cost_usd": 0.001,
            }

            # Pre-import so reconciler has something to process
            from tuition.tools.mcp_server import import_bank_csv_tool
            import_bank_csv_tool(str(bk))

            result = reconciliation_node({
                "billing_period": PERIOD,
                "errors": [],
                "total_llm_cost_usd": 0.0,
            })

        assert result["billing_result"]["charges_created"] == 1
        assert len(result["pending_charges"]) == 1
        assert result["pending_charges"][0]["family_name"] == "Smith Family"

    def test_no_pending_when_fully_reconciled(self, tmp_path):
        """PayPal txn matches -> reconcile succeeds -> _get_pending_charges returns []."""
        from tuition.agents.reconciliation import reconciliation_node

        engine = make_engine()
        with Session(engine) as session:
            _seed(session)

        pp = _write_paypal_csv(tmp_path)

        with patch("tuition.tools.mcp_server.get_session",
                   side_effect=lambda: _make_session(engine)), \
             patch("tuition.agents.reconciliation._get_open_families", return_value=[]), \
             patch("tuition.agents.reconciliation._get_pending_charges", return_value=[]):

            from tuition.tools.mcp_server import import_paypal_csv_tool
            import_paypal_csv_tool(str(pp))

            result = reconciliation_node({
                "billing_period": PERIOD,
                "errors": [],
                "total_llm_cost_usd": 0.0,
            })

        assert result["pending_charges"] == []
        assert result["reconcile_result"]["matched"] >= 1


# ── reminder_node ──────────────────────────────────────────────────────────────

class TestReminderNode:
    def test_drafts_one_reminder_per_charge(self):
        """Each pending charge produces one reminder via the template fallback."""
        from tuition.agents.reminder import reminder_node

        with _no_anthropic_import():
            result = reminder_node({
                "billing_period": PERIOD,
                "human_approved": True,
                "total_llm_cost_usd": 0.0,
                "pending_charges": [PENDING_CHARGE],
            })

        assert len(result["reminder_results"]) == 1
        r = result["reminder_results"][0]
        assert r["family_id"] == 1
        assert "Smith" in r["message_preview"] or "Alice" in r["message_preview"]

    def test_no_charges_no_reminders(self):
        """Empty pending_charges -> empty reminder_results."""
        from tuition.agents.reminder import reminder_node

        result = reminder_node({
            "billing_period": PERIOD,
            "human_approved": True,
            "total_llm_cost_usd": 0.0,
            "pending_charges": [],
        })
        assert result["reminder_results"] == []


# ── graph end-to-end (no interrupt) ───────────────────────────────────────────

class TestGraph:
    def test_end_to_end_no_interrupt(self, tmp_path):
        """Full graph run without interrupt — all nodes execute, state complete."""
        from tuition.agents.graph import build_graph

        engine = make_engine()
        with Session(engine) as session:
            _seed(session)

        pp = _write_paypal_csv(tmp_path)
        bk = _write_bank_csv(tmp_path)

        with patch("tuition.tools.mcp_server.get_session",
                   side_effect=lambda: _make_session(engine)), \
             patch("tuition.agents.reconciliation._get_pending_charges", return_value=[]), \
             patch("tuition.agents.reconciliation._get_open_families", return_value=[]), \
             _no_anthropic_import():

            graph = build_graph(interrupt_before_reminder=False)
            final = graph.invoke(
                {
                    "billing_period": PERIOD,
                    "paypal_csv_path": str(pp),
                    "bank_csv_path": str(bk),
                    "total_llm_cost_usd": 0.0,
                    "errors": [],
                    "human_approved": True,
                },
                {"configurable": {"thread_id": "test-e2e"}},
            )

        assert "ingestion_result" in final
        assert "billing_result" in final
        assert "reconcile_result" in final
        assert "reminder_results" in final
        assert final["ingestion_result"]["paypal"]["inserted"] == 1
