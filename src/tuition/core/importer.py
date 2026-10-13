"""
src/tuition/importer.py
-----------------------
Statement importer for Vrukshaa school.

Purpose
-------
Each month Vrukshaa receives payments via two channels:
  • PayPal  → you download a CSV from paypal.com/reports
  • Bank    → you download a CSV from your online banking portal

This module reads those CSV files and loads them into the `transactions`
table in the database — idempotently, so you can safely re-import the
same file without creating duplicate rows.

It does NOT try to match transactions to families or charges — that is
the reconciler's job (reconciler.py).  The importer just gets raw payment
data into the DB with minimal assumptions.

How idempotency works
---------------------
The `transactions` table has a unique constraint on (source, external_id).
  - For PayPal:  source="paypal",  external_id = PayPal's "Transaction ID" column
  - For Bank:    source="bank",    external_id = the "Reference" column

If a row already exists, we skip it.  If a CSV file has duplicate rows
(the same Transaction ID twice, as PayPal occasionally produces), we skip
the second occurrence at the CSV-parsing level before even hitting the DB.

Return value
------------
Both import functions return an ImportResult dataclass so callers and tests
can assert on what happened without parsing log output.

Usage
-----
  from pathlib import Path
  from tuition.importer import import_paypal_csv, import_bank_csv

  with Session(engine) as session:
      result = import_paypal_csv(session, Path("data/paypal_march2026.csv"))
      session.commit()
      print(f"Inserted {result.inserted}, skipped {result.skipped}")
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from tuition.db.models import Transaction, TransactionSource


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    """Summary of what an import run did.

    Attributes
    ----------
    inserted : int
        Number of new Transaction rows written to the DB.
    skipped : int
        Number of rows already in the DB (exact duplicate — same source + external_id).
    duplicate_in_csv : int
        Number of rows skipped because the same external_id appeared twice in the
        same CSV file (before the DB check).  PayPal sometimes produces these.
    errors : list[str]
        Human-readable messages for rows that couldn't be parsed (bad date, no ID, etc.).
        Malformed rows are skipped but counted, so the rest of the import continues.
    """

    inserted: int = 0
    skipped: int = 0
    duplicate_in_csv: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_rows_seen(self) -> int:
        return self.inserted + self.skipped + self.duplicate_in_csv + len(self.errors)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _already_exists(session: Session, source: TransactionSource, external_id: str) -> bool:
    """Return True if this (source, external_id) pair is already in the DB."""
    return session.scalars(
        select(Transaction).filter_by(source=source, external_id=external_id)
    ).first() is not None


def _parse_paypal_date(raw: str) -> date:
    """Parse PayPal's MM/DD/YYYY date strings into a Python date.

    PayPal exports dates as "01/05/2026" (month/day/year).
    We split on "/" and reconstruct the date — explicit parsing is safer
    than relying on locale-dependent datetime.strptime formats.

    Raises ValueError if the string can't be parsed.
    """
    parts = raw.strip().split("/")
    if len(parts) != 3:
        raise ValueError(f"Expected MM/DD/YYYY, got: {raw!r}")
    m, d, y = parts
    return date(int(y), int(m), int(d))


# ── PayPal importer ───────────────────────────────────────────────────────────

# Column names in a standard PayPal CSV export.
# PayPal uses a 32-column format; we only read the columns we care about.
PAYPAL_REQUIRED_COLS = {"Date", "Name", "Gross", "Transaction ID"}
PAYPAL_OPTIONAL_COLS = {"Subject", "Note"}


def import_paypal_csv(
    session: Session,
    csv_path: Path,
    *,
    recipient_email: str | None = None,
) -> ImportResult:
    """Import a PayPal transaction CSV export into the transactions table.

    Parameters
    ----------
    session : Session
        An open SQLAlchemy session. Caller must call session.commit().
    csv_path : Path
        Path to the PayPal CSV file on disk.
    recipient_email : str | None
        If given, only import rows where the "To Email Address" column matches
        this value.  Use this to filter out non-tuition transactions in a shared
        PayPal account.  If None, all rows are imported.

    PayPal CSV format notes
    -----------------------
    • 32 columns (mostly empty for simple payments)
    • Dates: MM/DD/YYYY
    • Amounts: quoted strings like "40.00" (no currency symbol)
    • Column headers appear on row 1, quoted: "Date","Name","Gross",...
    • The "Transaction ID" column is PayPal's unique identifier per transaction
    • "Subject" and "Note" are the memo fields parents fill in
    • Fees are in the "Fee" column (negative); we use "Gross" (what arrived)

    Returns
    -------
    ImportResult
    """
    result = ImportResult()
    seen_ids: set[str] = set()  # CSV-level duplicate detection

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)

        # Header row — strip surrounding quotes that PayPal includes
        raw_headers = next(reader, None)
        if raw_headers is None:
            result.errors.append("Empty file — no header row found.")
            return result

        headers = [h.strip().strip('"') for h in raw_headers]
        col = {h: i for i, h in enumerate(headers)}

        # Validate required columns exist
        missing = PAYPAL_REQUIRED_COLS - set(col)
        if missing:
            result.errors.append(
                f"Missing required PayPal columns: {sorted(missing)}"
            )
            return result

        for line_num, row in enumerate(reader, start=2):
            if not row:
                continue  # blank line

            # ── Get the external_id ───────────────────────────────────────
            ext_id = row[col["Transaction ID"]].strip()
            if not ext_id:
                result.errors.append(f"Line {line_num}: empty Transaction ID — skipped.")
                continue

            # ── CSV-level duplicate guard ─────────────────────────────────
            if ext_id in seen_ids:
                result.duplicate_in_csv += 1
                continue
            seen_ids.add(ext_id)

            # ── Optional: recipient email filter ─────────────────────────
            if recipient_email and "To Email Address" in col:
                to_email = row[col["To Email Address"]].strip()
                if to_email and to_email.lower() != recipient_email.lower():
                    result.skipped += 1
                    continue

            # ── DB-level idempotency check ────────────────────────────────
            if _already_exists(session, TransactionSource.PAYPAL, ext_id):
                result.skipped += 1
                continue

            # ── Parse date ────────────────────────────────────────────────
            try:
                paid_on = _parse_paypal_date(row[col["Date"]])
            except (ValueError, IndexError) as exc:
                result.errors.append(f"Line {line_num} ({ext_id}): bad date — {exc}")
                continue

            # ── Parse amount ─────────────────────────────────────────────
            try:
                amount = Decimal(row[col["Gross"]].strip())
                if amount <= 0:
                    result.errors.append(
                        f"Line {line_num} ({ext_id}): non-positive amount {amount} — skipped."
                    )
                    continue
            except (InvalidOperation, IndexError) as exc:
                result.errors.append(f"Line {line_num} ({ext_id}): bad amount — {exc}")
                continue

            # ── Build memo from Subject + Note ────────────────────────────
            memo_parts: list[str] = []
            for memo_col in ("Subject", "Note"):
                if memo_col in col:
                    val = row[col[memo_col]].strip()
                    if val:
                        memo_parts.append(val)
            memo = " | ".join(memo_parts) if memo_parts else None

            # ── Insert ────────────────────────────────────────────────────
            txn = Transaction(
                source=TransactionSource.PAYPAL,
                external_id=ext_id,
                paid_on=paid_on,
                amount=amount,
                payer_name=row[col["Name"]].strip() or None,
                memo=memo,
                # family_id left NULL — reconciler assigns it later
            )
            session.add(txn)
            result.inserted += 1

    return result


# ── Bank importer ─────────────────────────────────────────────────────────────

# Column names in our bank CSV format.
# Real bank CSVs vary by institution; this matches the format we defined in
# data/seed/bank_jan2026.csv.  If your bank uses different column names,
# subclass or modify this function, or pre-convert the CSV to this format.
BANK_REQUIRED_COLS = {"Date", "Description", "Amount", "Reference"}


def import_bank_csv(session: Session, csv_path: Path) -> ImportResult:
    """Import a bank statement CSV into the transactions table.

    Parameters
    ----------
    session : Session
        An open SQLAlchemy session. Caller must call session.commit().
    csv_path : Path
        Path to the bank CSV file on disk.

    Bank CSV format (matches data/seed/bank_jan2026.csv)
    -----------------------------------------------------
      Date        YYYY-MM-DD
      Description Free-text bank description (often payer name + reference)
      Amount      Decimal string like "40.00"  (credits only — we skip debits)
      Reference   Bank's unique reference number  (our external_id)
      Type        "Credit" or "Debit"

    Only "Credit" rows are imported (incoming payments).  Debits are ignored.

    Returns
    -------
    ImportResult
    """
    result = ImportResult()

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)

        # Validate required columns
        if reader.fieldnames is None:
            result.errors.append("Empty file — no header row found.")
            return result

        fieldnames = [f.strip() for f in reader.fieldnames]
        missing = BANK_REQUIRED_COLS - set(fieldnames)
        if missing:
            result.errors.append(
                f"Missing required bank columns: {sorted(missing)}"
            )
            return result

        for line_num, raw_row in enumerate(reader, start=2):
            # Strip whitespace from all values
            row = {k.strip(): v.strip() for k, v in raw_row.items() if k}

            # ── Skip debits ───────────────────────────────────────────────
            txn_type = row.get("Type", "").lower()
            if txn_type and txn_type != "credit":
                continue

            # ── Get external_id ───────────────────────────────────────────
            ext_id = row.get("Reference", "").strip()
            if not ext_id:
                result.errors.append(f"Line {line_num}: empty Reference — skipped.")
                continue

            # ── DB-level idempotency (bank files rarely have CSV duplicates) ─
            if _already_exists(session, TransactionSource.BANK, ext_id):
                result.skipped += 1
                continue

            # ── Parse date (YYYY-MM-DD) ────────────────────────────────────
            try:
                paid_on = date.fromisoformat(row["Date"])
            except (ValueError, KeyError) as exc:
                result.errors.append(f"Line {line_num} ({ext_id}): bad date — {exc}")
                continue

            # ── Parse amount ──────────────────────────────────────────────
            try:
                amount = Decimal(row["Amount"])
                if amount <= 0:
                    result.errors.append(
                        f"Line {line_num} ({ext_id}): non-positive amount {amount} — skipped."
                    )
                    continue
            except (InvalidOperation, KeyError) as exc:
                result.errors.append(f"Line {line_num} ({ext_id}): bad amount — {exc}")
                continue

            # ── Description becomes both payer_name and memo ──────────────
            description = row.get("Description", "")

            # ── Insert ────────────────────────────────────────────────────
            txn = Transaction(
                source=TransactionSource.BANK,
                external_id=ext_id,
                paid_on=paid_on,
                amount=amount,
                payer_name=description or None,
                memo=description or None,
                # family_id left NULL — reconciler assigns it later
            )
            session.add(txn)
            result.inserted += 1

    return result
