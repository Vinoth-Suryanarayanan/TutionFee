"""
scripts/seed.py
---------------
Populate the SQLite dev database with Vrukshaa school mock data.

What this script does, in order:
  1. Reads data/seed/families.json  -> inserts Family, Guardian, Student rows
  2. Reads data/seed/enrollments.json -> inserts Enrollment rows
  3. Creates charges for billing period 2026-01-01 (one per active enrollment)
  4. Reads data/seed/paypal_jan2026.csv -> inserts Transaction rows (PayPal)
  5. Reads data/seed/bank_jan2026.csv  -> inserts Transaction rows (bank)

Idempotent: running it twice does not duplicate data.

Usage:
  python scripts/seed.py                    # uses dev.db (default)
  DATABASE_URL=sqlite:///other.db python scripts/seed.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

# make the package importable when run from project root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tuition.db.models import (
    Base,
    Charge,
    ChargeStatus,
    Enrollment,
    Family,
    Guardian,
    Student,
    Transaction,
    TransactionSource,
)

# paths
ROOT = Path(__file__).parent.parent
SEED_DIR = ROOT / "data" / "seed"
FAMILIES_JSON = SEED_DIR / "families.json"
ENROLLMENTS_JSON = SEED_DIR / "enrollments.json"
PAYPAL_CSV = SEED_DIR / "paypal_jan2026.csv"
BANK_CSV = SEED_DIR / "bank_jan2026.csv"

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{ROOT}/dev.db")

BILLING_PERIOD = date(2026, 1, 1)


def get_or_none(session, model, **kwargs):
    """Return first matching row or None."""
    return session.scalars(select(model).filter_by(**kwargs)).first()


def log(msg: str) -> None:
    print(f"  {msg}")


def seed_families(session) -> None:
    """
    Inserts Family, Guardian, and Student rows from families.json.

    families.json structure:
      [
        {
          "external_key": "FAM001",
          "display_name": "Smith Family",
          "guardians": [
            {
              "full_name": "Robert Smith",
              "email": "robert.smith@gmail.com",
              "phone": "408-555-0101",
              "relation_to_student": "father",
              "is_primary": true,
              "preferred_channel": "email",
              "paypal_name": "Robert Smith"    <- name used in PayPal exports
            }
          ],
          "students": [
            { "external_key": "STU001", "first_name": "Alice", "last_name": "Smith" }
          ]
        }
      ]

    Note: paypal_name is stored in the JSON for matching reference but is
    NOT a DB column. The matching agent compares the CSV payer_name to
    guardian.full_name (and family.display_name) at runtime.
    """
    data = json.loads(FAMILIES_JSON.read_text())

    for fam_data in data:
        key = fam_data["external_key"]

        family = get_or_none(session, Family, external_key=key)
        if family is None:
            family = Family(
                external_key=key,
                display_name=fam_data["display_name"],
            )
            session.add(family)
            session.flush()
            log(f"Created family {key}: {fam_data['display_name']}")
        else:
            log(f"Skipped family {key} (already exists)")

        for g_data in fam_data.get("guardians", []):
            existing = get_or_none(
                session, Guardian,
                family_id=family.id,
                email=g_data["email"],
            )
            if existing is None:
                guardian = Guardian(
                    family_id=family.id,
                    full_name=g_data["full_name"],
                    email=g_data["email"],
                    phone=g_data.get("phone"),
                    relation_to_student=g_data.get("relation_to_student"),
                    is_primary=g_data.get("is_primary", False),
                    preferred_channel=g_data.get("preferred_channel", "email"),
                )
                session.add(guardian)
                log(f"  + guardian {g_data['full_name']}")

        for s_data in fam_data.get("students", []):
            s_key = s_data["external_key"]
            existing = get_or_none(session, Student, external_key=s_key)
            if existing is None:
                student = Student(
                    family_id=family.id,
                    external_key=s_key,
                    first_name=s_data["first_name"],
                    last_name=s_data["last_name"],
                )
                session.add(student)
                log(f"  + student {s_key}: {s_data['first_name']} {s_data['last_name']}")

    session.flush()


def seed_enrollments(session) -> dict:
    """
    Inserts Enrollment rows from enrollments.json.

    Returns a dict mapping (student_key, program) -> Enrollment.

    enrollments.json structure:
      [
        {
          "student_key": "STU001",
          "program": "Carnatic",
          "monthly_fee": "40.00",
          "start_date": "2025-09-01"
        },
        ...
      ]

    Note: STU004 (Ethan) appears twice (Carnatic + Yoga) because he takes
    both classes. That means two enrollments -> two charges -> $80/month total.
    """
    data = json.loads(ENROLLMENTS_JSON.read_text())
    enrollment_map = {}

    for e_data in data:
        if "_comment" in e_data:
            continue

        s_key = e_data["student_key"]
        program = e_data["program"]

        student = get_or_none(session, Student, external_key=s_key)
        if student is None:
            raise ValueError(f"Student {s_key} not found - run seed_families() first.")

        existing = get_or_none(
            session, Enrollment,
            student_id=student.id,
            program=program,
        )
        if existing is None:
            start = date.fromisoformat(e_data["start_date"])
            enrollment = Enrollment(
                student_id=student.id,
                program=program,
                monthly_fee=Decimal(e_data["monthly_fee"]),
                start_date=start,
            )
            session.add(enrollment)
            session.flush()
            log(f"Created enrollment {s_key} -> {program} @ ${e_data['monthly_fee']}/mo")
            enrollment_map[(s_key, program)] = enrollment
        else:
            log(f"Skipped enrollment {s_key}/{program} (already exists)")
            enrollment_map[(s_key, program)] = existing

    return enrollment_map


def seed_charges(session, enrollment_map, billing_period) -> None:
    """
    Creates one Charge row per active enrollment for the given billing_period.

    Active = enrollment.start_date <= billing_period and no end_date set.

    Why charges exist as rows (not computed on the fly):
    We need a concrete "what is owed" record so the matching agent can link
    a payment -> allocation -> charge. Without Charge rows there is nothing
    to reconcile against.
    """
    for (s_key, program), enrollment in enrollment_map.items():
        if enrollment.start_date > billing_period:
            log(f"Skipped charge {s_key}/{program}: start_date after billing period")
            continue

        existing = get_or_none(
            session, Charge,
            enrollment_id=enrollment.id,
            billing_period=billing_period,
        )
        if existing is None:
            charge = Charge(
                enrollment_id=enrollment.id,
                family_id=enrollment.student.family_id,
                billing_period=billing_period,
                amount_due=enrollment.monthly_fee,
                status=ChargeStatus.PENDING,
            )
            session.add(charge)
            log(f"Created charge {s_key}/{program} {billing_period} = ${enrollment.monthly_fee}")
        else:
            log(f"Skipped charge {s_key}/{program}/{billing_period} (already exists)")


def seed_paypal(session) -> None:
    """
    Reads paypal_jan2026.csv and inserts Transaction rows.

    PayPal CSV has 32 columns. Key columns we use:
      Date            -> paid_on  (MM/DD/YYYY format)
      Name            -> payer_name
      Gross           -> amount
      Transaction ID  -> external_id (our idempotency key)
      Subject + Note  -> memo (concatenated)

    Idempotency:
      - CSV-level: same Transaction ID twice in the file -> skip second row
      - DB-level: same (source, external_id) already in DB -> skip
    """
    seen_ids = set()
    duplicate_count = 0

    with open(PAYPAL_CSV, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        headers = [h.strip('"') for h in next(reader)]
        col = {h: i for i, h in enumerate(headers)}

        for row in reader:
            if not row:
                continue

            ext_id = row[col["Transaction ID"]].strip()
            if not ext_id:
                continue

            if ext_id in seen_ids:
                log(f"  ! Skipping CSV duplicate: {ext_id}")
                duplicate_count += 1
                continue
            seen_ids.add(ext_id)

            existing = get_or_none(
                session, Transaction,
                source=TransactionSource.PAYPAL,
                external_id=ext_id,
            )
            if existing is not None:
                log(f"Skipped PayPal txn {ext_id} (already in DB)")
                continue

            # Parse MM/DD/YYYY -> date
            raw_date = row[col["Date"]].strip()
            m, d, y = raw_date.split("/")
            paid_on = date(int(y), int(m), int(d))

            memo_parts = []
            if "Subject" in col and row[col["Subject"]].strip():
                memo_parts.append(row[col["Subject"]].strip())
            if "Note" in col and row[col["Note"]].strip():
                memo_parts.append(row[col["Note"]].strip())

            txn = Transaction(
                source=TransactionSource.PAYPAL,
                external_id=ext_id,
                paid_on=paid_on,
                amount=Decimal(row[col["Gross"]].strip()),
                payer_name=row[col["Name"]].strip(),
                memo=" | ".join(memo_parts) if memo_parts else None,
            )
            session.add(txn)
            log(f"Inserted PayPal {ext_id}: {txn.payer_name} ${txn.amount}")

    if duplicate_count:
        log(f"  -> {duplicate_count} CSV duplicate row(s) skipped")


def seed_bank(session) -> None:
    """
    Reads bank_jan2026.csv and inserts Transaction rows.

    Bank CSV columns: Date (YYYY-MM-DD), Description, Amount, Reference, Type

    The bank Reference column becomes external_id.
    Description becomes both payer_name and memo.

    Note: BNK-JAN003 (Karen Johnson, $80) is a cross-channel duplicate of
    PP-JAN010. The seed inserts it anyway - detecting and flagging this
    duplicate is the matching agent's job, not the importer's.
    """
    with open(BANK_CSV, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)

        for row in reader:
            ext_id = row["Reference"].strip()
            if not ext_id:
                continue

            existing = get_or_none(
                session, Transaction,
                source=TransactionSource.BANK,
                external_id=ext_id,
            )
            if existing is not None:
                log(f"Skipped bank txn {ext_id} (already in DB)")
                continue

            paid_on = date.fromisoformat(row["Date"].strip())

            txn = Transaction(
                source=TransactionSource.BANK,
                external_id=ext_id,
                paid_on=paid_on,
                amount=Decimal(row["Amount"].strip()),
                payer_name=row["Description"].strip(),
                memo=row["Description"].strip(),
            )
            session.add(txn)
            log(f"Inserted bank {ext_id}: {txn.payer_name} ${txn.amount}")


def main() -> None:
    print(f"Database: {DATABASE_URL}")
    engine = create_engine(DATABASE_URL, echo=False)

    # Create tables if they don\'t exist.
    # In production we run Alembic migrations; this is a dev convenience.
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        print("\n-- Step 1: Families, guardians, students --")
        seed_families(session)

        print("\n-- Step 2: Enrollments --")
        enrollment_map = seed_enrollments(session)

        print(f"\n-- Step 3: Charges for {BILLING_PERIOD} --")
        seed_charges(session, enrollment_map, BILLING_PERIOD)

        print("\n-- Step 4: PayPal transactions --")
        seed_paypal(session)

        print("\n-- Step 5: Bank transactions --")
        seed_bank(session)

        session.commit()

    print("\nSeed complete.")


if __name__ == "__main__":
    main()
