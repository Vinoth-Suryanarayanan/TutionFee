"""
src/tuition/billing.py
----------------------
Billing engine for Vrukshaa school.

What this module does:
  Given a billing_period (first day of a month, e.g. date(2026, 1, 1)),
  it finds every active enrollment and creates one Charge row per enrollment
  — but only if that charge doesn't already exist.

Key concept — "active on billing_period":
  An enrollment is active if:
    enrollment.start_date <= billing_period
    AND (enrollment.end_date is None   ← still enrolled
         OR enrollment.end_date >= billing_period)   ← withdrawn after this month

  The Enrollment model already has an is_active_on(day) method that encodes
  this logic; we call it here instead of re-implementing the condition.

Why we create Charge rows at all:
  A Charge is the formal "this student owes $40 for Carnatic in January" record.
  Without it, the matching agent would have nothing to reconcile payments against.
  Charges are immutable once created — the matching engine updates their status
  (PENDING → PARTIAL → PAID) by adding Allocation rows.

Idempotency:
  The DB has a unique constraint on (enrollment_id, billing_period).
  run_billing() also checks before inserting, so calling it twice for the same
  period is safe — the second call returns an empty list (nothing new to create).

Usage:
  from tuition.billing import run_billing
  from datetime import date

  new_charges = run_billing(session, date(2026, 1, 1))
  session.commit()
  print(f"Created {len(new_charges)} charges")
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tuition.db.models import Charge, ChargeStatus, Enrollment


def run_billing(session: Session, billing_period: date) -> list[Charge]:
    """
    Create Charge rows for every active enrollment in billing_period.

    Parameters
    ----------
    session : Session
        An open SQLAlchemy session.  The caller is responsible for commit().
    billing_period : date
        First day of the billing month (e.g. date(2026, 1, 1)).
        Charges are keyed on (enrollment_id, billing_period), so you can safely
        call this once per month without duplicating data.

    Returns
    -------
    list[Charge]
        Only the *newly created* Charge objects (already added to session,
        not yet committed).  Already-existing charges are silently skipped.

    Why selectinload?
    -----------------
    We need enrollment.student.family_id to fill the denormalised family_id
    column on Charge.  selectinload tells SQLAlchemy to fetch all Student rows
    for the enrollments in one extra SELECT instead of N lazy queries.
    """
    # Load all enrollments, eagerly fetching their student (to get family_id)
    enrollments: list[Enrollment] = session.scalars(
        select(Enrollment).options(selectinload(Enrollment.student))
    ).all()

    new_charges: list[Charge] = []

    for enrollment in enrollments:
        # ── 1. Is this enrollment active during billing_period? ──────────────
        #
        # is_active_on checks:
        #   enrollment.start_date <= billing_period
        #   enrollment.end_date is None  OR  enrollment.end_date >= billing_period
        #
        if not enrollment.is_active_on(billing_period):
            # Student started after this period, or withdrew before it — skip.
            continue

        # ── 2. Does a charge already exist for this enrollment + period? ─────
        #
        # The DB has UniqueConstraint("enrollment_id", "billing_period"), so
        # inserting a duplicate would raise an IntegrityError.  We check first
        # so we can skip gracefully instead of blowing up.
        #
        existing = session.scalars(
            select(Charge).filter_by(
                enrollment_id=enrollment.id,
                billing_period=billing_period,
            )
        ).first()

        if existing is not None:
            # Already billed — nothing to do for this enrollment.
            continue

        # ── 3. Create the Charge ─────────────────────────────────────────────
        #
        # family_id is denormalised on Charge (copied from the student's family)
        # so the matching engine can filter "all charges for FAM002" without
        # a multi-hop join through enrollments → students → families.
        #
        charge = Charge(
            enrollment_id=enrollment.id,
            family_id=enrollment.student.family_id,
            billing_period=billing_period,
            amount_due=enrollment.monthly_fee,  # Decimal, copied from enrollment
            status=ChargeStatus.PENDING,
        )
        session.add(charge)
        new_charges.append(charge)

    # Flush so the new rows get their DB-assigned ids before we return them.
    # (The caller still needs to call session.commit() to persist.)
    if new_charges:
        session.flush()

    return new_charges
