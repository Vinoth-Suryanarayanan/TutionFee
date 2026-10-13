"""
src/tuition/tools/mcp_server.py
--------------------------
FastMCP server for Vrukshaa school.

Purpose
-------
Exposes the three core operations as MCP tools so that:
  • LangGraph agents (Step 8) can call them as structured function calls
  • Claude itself can call them directly via the MCP protocol
  • Any MCP-compatible client (Cursor, Claude Desktop, custom agent) can use them

Tools exposed
-------------
  run_billing(billing_period)           → list of new Charge summaries
  import_paypal_csv(csv_path, ...)      → ImportResult summary dict
  import_bank_csv(csv_path)             → ImportResult summary dict
  reconcile_all_pending(billing_period) → list of ReconcileResult summary dicts

Design notes
------------
• All money values are returned as strings ("40.00") not floats — JSON cannot
  represent Decimal exactly, and we want callers to reconstruct Decimal if needed.
• Dates are returned as ISO strings ("2026-01-01") for the same reason.
• The server creates its own Session per tool call and commits on success.
  This means each call is atomic — a partial failure rolls back cleanly.
• DATABASE_URL is read from the environment (via config.py).  For tests we
  patch the session factory.

Usage (CLI)
-----------
  # From project root with venv active:
  python -m tuition.tools.mcp_server

  # Or via make:
  make mcp

Usage (MCP client config)
-------------------------
  {
    "mcpServers": {
      "vrukshaa": {
        "command": "python",
        "args": ["-m", "tuition.mcp_server"],
        "cwd": "/path/to/TutionFee"
      }
    }
  }
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from mcp.server.mcpserver import MCPServer as FastMCP

from tuition.core.billing import run_billing
from tuition.db.session import session_scope as get_session
from tuition.core.importer import import_bank_csv, import_paypal_csv
from tuition.core.reconciler import reconcile_all_pending

# ── Server instance ────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="vrukshaa",
    instructions=(
        "Vrukshaa school tuition management. "
        "Call run_billing first to generate charges for a period, "
        "then import_paypal_csv / import_bank_csv to load payments, "
        "then reconcile_all_pending to match payments to charges."
    ),
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_billing_period(billing_period: str) -> date:
    """Parse an ISO date string (YYYY-MM-DD) into a date.

    Raises ValueError with a friendly message if the string is malformed.
    The day component is normalised to 1 (first of the month).
    """
    try:
        d = date.fromisoformat(billing_period)
    except ValueError:
        raise ValueError(
            f"billing_period must be YYYY-MM-DD (e.g. '2026-01-01'), got: {billing_period!r}"
        )
    return date(d.year, d.month, 1)


# ── Tool: run_billing ──────────────────────────────────────────────────────────

@mcp.tool()
def run_billing_tool(billing_period: str) -> dict:
    """Generate monthly Charge rows for all active enrollments.

    Creates one Charge per active Enrollment for the given billing period.
    Safe to call multiple times — already-existing charges are skipped
    (idempotent via UNIQUE(enrollment_id, billing_period) constraint).

    Parameters
    ----------
    billing_period : str
        ISO date string for the first day of the billing month.
        Example: "2026-01-01" for January 2026.

    Returns
    -------
    dict with keys:
        period        : str   — billing period used (YYYY-MM-DD)
        charges_created : int — number of new Charge rows written
        charges       : list  — summary of each new charge:
                          { charge_id, family_id, enrollment_id,
                            amount_due, status }
    """
    period = _parse_billing_period(billing_period)

    with get_session() as session:
        new_charges = run_billing(session, period)
        session.commit()

        return {
            "period": period.isoformat(),
            "charges_created": len(new_charges),
            "charges": [
                {
                    "charge_id": c.id,
                    "family_id": c.family_id,
                    "enrollment_id": c.enrollment_id,
                    "amount_due": str(c.amount_due),
                    "status": c.status,
                }
                for c in new_charges
            ],
        }


# ── Tool: import_paypal_csv ────────────────────────────────────────────────────

@mcp.tool()
def import_paypal_csv_tool(
    csv_path: str,
    recipient_email: str = "",
) -> dict:
    """Import a PayPal CSV export into the transactions table.

    Reads the CSV at csv_path and inserts new Transaction rows.
    Idempotent — rows already in the DB (same Transaction ID) are skipped.

    Parameters
    ----------
    csv_path : str
        Absolute path to the PayPal CSV file on disk.
        Download from paypal.com → Activity → Download → CSV.
    recipient_email : str
        Optional. If given, only rows where "To Email Address" matches
        this value are imported.  Leave empty to import all rows.
        Use this when your PayPal account receives non-tuition payments too.

    Returns
    -------
    dict with keys:
        inserted         : int       — new rows added
        skipped          : int       — rows already in DB
        duplicate_in_csv : int       — same Transaction ID appeared twice in CSV
        errors           : list[str] — parse failures (bad date, empty ID, etc.)
        total_rows_seen  : int       — sum of all four counters
    """
    path = Path(csv_path)
    if not path.exists():
        return {
            "inserted": 0,
            "skipped": 0,
            "duplicate_in_csv": 0,
            "errors": [f"File not found: {csv_path}"],
            "total_rows_seen": 0,
        }

    with get_session() as session:
        result = import_paypal_csv(
            session,
            path,
            recipient_email=recipient_email or None,
        )
        session.commit()

    return {
        "inserted": result.inserted,
        "skipped": result.skipped,
        "duplicate_in_csv": result.duplicate_in_csv,
        "errors": result.errors,
        "total_rows_seen": result.total_rows_seen,
    }


# ── Tool: import_bank_csv ──────────────────────────────────────────────────────

@mcp.tool()
def import_bank_csv_tool(csv_path: str) -> dict:
    """Import a bank statement CSV into the transactions table.

    Reads the CSV at csv_path and inserts new Transaction rows for credit rows.
    Debit rows are ignored.  Idempotent — rows already in the DB are skipped.

    The CSV must have these columns: Date, Description, Amount, Reference, Type
    Dates must be YYYY-MM-DD.  Type must be "Credit" or "Debit".

    Parameters
    ----------
    csv_path : str
        Absolute path to the bank CSV file on disk.

    Returns
    -------
    dict with keys:
        inserted         : int       — new rows added
        skipped          : int       — rows already in DB
        duplicate_in_csv : int       — (always 0 for bank — bank files rarely dup)
        errors           : list[str] — parse failures
        total_rows_seen  : int
    """
    path = Path(csv_path)
    if not path.exists():
        return {
            "inserted": 0,
            "skipped": 0,
            "duplicate_in_csv": 0,
            "errors": [f"File not found: {csv_path}"],
            "total_rows_seen": 0,
        }

    with get_session() as session:
        result = import_bank_csv(session, path)
        session.commit()

    return {
        "inserted": result.inserted,
        "skipped": result.skipped,
        "duplicate_in_csv": result.duplicate_in_csv,
        "errors": result.errors,
        "total_rows_seen": result.total_rows_seen,
    }


# ── Tool: reconcile_all_pending ────────────────────────────────────────────────

@mcp.tool()
def reconcile_all_pending_tool(
    billing_period: str,
    source: str = "",
) -> dict:
    """Match all unreconciled transactions to family charges.

    Processes every transaction that has no Allocation rows yet.
    For each transaction, tries to identify the family (by payer name),
    finds their open charges for the period, and creates Allocation rows.
    Idempotent — transactions already allocated are skipped.

    Parameters
    ----------
    billing_period : str
        ISO date string for the billing month: "2026-01-01".
    source : str
        Optional. Filter to one payment source: "paypal" or "bank".
        Leave empty to process all sources.

    Returns
    -------
    dict with keys:
        period           : str  — billing period used
        total            : int  — number of transactions processed
        matched          : int  — successfully matched to a family + charges
        partial          : int  — matched but payment exceeded all open charges
        unmatched        : int  — couldn't identify the family
        no_charges       : int  — family found but no open charges
        skipped          : int  — already had allocations
        results          : list — one entry per transaction:
            { transaction_id, external_id, outcome,
              matched_family_id, allocations_created, notes }
    """
    from tuition.db.models import TransactionSource

    period = _parse_billing_period(billing_period)

    # Parse optional source filter
    source_filter = None
    if source:
        source_lower = source.lower()
        if source_lower == "paypal":
            source_filter = TransactionSource.PAYPAL
        elif source_lower == "bank":
            source_filter = TransactionSource.BANK
        else:
            return {
                "period": period.isoformat(),
                "total": 0,
                "matched": 0,
                "partial": 0,
                "unmatched": 0,
                "no_charges": 0,
                "skipped": 0,
                "results": [],
                "error": f"Unknown source {source!r}. Use 'paypal' or 'bank'.",
            }

    with get_session() as session:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from tuition.db.models import Transaction

        # Count already-reconciled transactions (have allocations) so we can
        # report them as skipped — reconcile_all_pending only returns unreconciled ones.
        pre_query = (
            select(Transaction)
            .options(selectinload(Transaction.allocations))
        )
        if source_filter is not None:
            pre_query = pre_query.filter(Transaction.source == source_filter)
        all_txns = session.scalars(pre_query).all()
        already_reconciled = sum(1 for t in all_txns if t.allocations)

        results = reconcile_all_pending(session, period, source=source_filter)
        session.commit()

    # Tally outcomes
    counts: dict[str, int] = {
        "matched": 0,
        "partial": 0,
        "unmatched": 0,
        "no_charges": 0,
        "skipped": already_reconciled,  # transactions that already had allocations
    }
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1

    return {
        "period": period.isoformat(),
        "total": len(results),
        **counts,
        "results": [
            {
                "transaction_id": r.transaction_id,
                "external_id": r.external_id,
                "outcome": r.outcome,
                "matched_family_id": r.matched_family_id,
                "allocations_created": r.allocations_created,
                "notes": r.notes,
            }
            for r in results
        ],
    }


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
