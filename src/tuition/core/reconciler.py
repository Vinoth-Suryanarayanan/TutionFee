"""
src/tuition/reconciler.py
--------------------------
Reconciliation engine for Vrukshaa school.

Purpose
-------
After the importer loads transactions into the DB, this module does the
"matching" work: given a Transaction, figure out which family it belongs to
and which Charge(s) it covers — then write Allocation rows and update
Charge statuses.

This is deterministic, rule-based matching — no LLM required.  The LangGraph
agents (Step 8) will call these same functions and optionally use an LLM only
for ambiguous cases (name mismatches, split payments, etc.).

Matching strategy
-----------------
Step 1 — Name match (family lookup):
  Compare the transaction's payer_name against:
    a. Guardian.full_name (exact, then case-insensitive)
    b. Family.display_name (same)
  Return the family if exactly one match is found.

Step 2 — Charge matching (given a family):
  Find all PENDING or PARTIAL Charges for that family in the billing period.
  Apply the transaction amount across those charges in order (oldest first),
  creating one Allocation row per charge covered, until the money runs out.

Step 3 — Status update:
  After allocations are written, recompute each affected Charge's status:
    amount_paid == amount_due  → PAID
    0 < amount_paid < amount_due → PARTIAL
    (amount_paid is the sum of its Allocation rows)

What this module does NOT do
-----------------------------
  • Does not handle name mismatches (e.g. "Raj Patel" paying for "Priya Patel").
    Those are flagged as unmatched — the LangGraph agent resolves them.
  • Does not detect cross-channel duplicates (same payment via PayPal + bank).
    That's the reconciliation agent's job, not this deterministic engine.
  • Does not send reminders — that's the reminder agent.

Return type
-----------
ReconcileResult records what happened for each transaction processed.

Usage
-----
  from tuition.reconciler import reconcile_transaction, reconcile_all_pending

  with Session(engine) as session:
      # Reconcile one transaction
      result = reconcile_transaction(session, txn_id=42, billing_period=date(2026,1,1))

      # Reconcile all unmatched transactions for a period
      results = reconcile_all_pending(session, billing_period=date(2026,1,1))
      session.commit()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tuition.db.models import (
    Allocation,
    Charge,
    ChargeStatus,
    Family,
    Guardian,
    Transaction,
    TransactionSource,
)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ReconcileResult:
    """What happened when we tried to reconcile one transaction.

    Attributes
    ----------
    transaction_id : int
        The DB id of the Transaction we processed.
    external_id : str
        The source system's ID (PayPal/bank reference) — for log messages.
    matched_family_id : int | None
        The family we matched this transaction to, or None if unmatched.
    allocations_created : int
        Number of Allocation rows written.
    charges_updated : list[int]
        IDs of Charge rows whose status was updated.
    outcome : str
        Short human-readable label:
          "matched"    → matched to a family and allocations created
          "partial"    → matched, but amount exceeded all open charges
          "unmatched"  → couldn't identify the family
          "skipped"    → transaction already had allocations (already processed)
          "no_charges" → family found but no open charges for this period
    notes : list[str]
        Additional details about what happened (useful for debugging).
    """

    transaction_id: int
    external_id: str
    matched_family_id: int | None = None
    allocations_created: int = 0
    charges_updated: list[int] = field(default_factory=list)
    outcome: str = "unmatched"
    notes: list[str] = field(default_factory=list)


# ── Family lookup ─────────────────────────────────────────────────────────────

def find_family_by_payer_name(
    session: Session, payer_name: str
) -> Family | None:
    """Try to identify the family a payer belongs to.

    Strategy (in order):
      1. Exact match on Guardian.full_name
      2. Case-insensitive match on Guardian.full_name
      3. Case-insensitive match on Family.display_name
         (catches cases like "Smith Family" in the bank description)

    Returns the matched Family, or None if zero or multiple families matched
    (ambiguous matches are treated as unmatched — the agent resolves them).

    Why not fuzzy matching here?
    Fuzzy (Levenshtein) matching is more expensive and more prone to false
    positives on short names.  We keep this deterministic and fast; the
    LangGraph agent calls the LLM for the hard cases.
    """
    name_lower = payer_name.strip().lower()
    if not name_lower:
        return None

    # ── 1. Exact match on guardian full_name ──────────────────────────────
    exact_guardians = session.scalars(
        select(Guardian)
        .filter_by(full_name=payer_name.strip())
        .options(selectinload(Guardian.family))
    ).all()
    if len(exact_guardians) == 1:
        return exact_guardians[0].family
    if len(exact_guardians) > 1:
        # Multiple families share this guardian name — can't decide
        return None

    # ── 2. Case-insensitive match on guardian full_name ───────────────────
    all_guardians = session.scalars(
        select(Guardian).options(selectinload(Guardian.family))
    ).all()

    ci_families = {
        g.family
        for g in all_guardians
        if g.full_name.lower() == name_lower
    }
    if len(ci_families) == 1:
        return next(iter(ci_families))
    if len(ci_families) > 1:
        return None

    # ── 3. Case-insensitive match on family display_name ──────────────────
    all_families = session.scalars(select(Family)).all()
    name_families = [
        f for f in all_families
        if f.display_name.lower() == name_lower
    ]
    if len(name_families) == 1:
        return name_families[0]

    return None


# ── Charge allocation ─────────────────────────────────────────────────────────

def _open_charges_for_family(
    session: Session, family_id: int, billing_period: date
) -> list[Charge]:
    """Return PENDING and PARTIAL charges for a family in billing_period, oldest first."""
    return session.scalars(
        select(Charge)
        .filter(
            Charge.family_id == family_id,
            Charge.billing_period == billing_period,
            Charge.status.in_([ChargeStatus.PENDING, ChargeStatus.PARTIAL]),
        )
        .options(selectinload(Charge.allocations))
        .order_by(Charge.id)  # consistent ordering
    ).all()


def _update_charge_status(charge: Charge) -> None:
    """Recompute and set charge.status based on its current allocations.

    Called after writing new Allocation rows so the status is always
    consistent with the actual money recorded.
    """
    paid = charge.amount_paid
    if paid >= charge.amount_due:
        charge.status = ChargeStatus.PAID
    elif paid > Decimal("0.00"):
        charge.status = ChargeStatus.PARTIAL
    # If paid is 0 we leave the status as PENDING (shouldn't happen here,
    # but defensive in case an allocation of 0 somehow slips through)


def allocate_to_charges(
    session: Session,
    transaction: Transaction,
    charges: list[Charge],
) -> list[Allocation]:
    """Apply transaction money across the given charges, in order.

    Creates Allocation rows until the transaction amount is exhausted or
    all charges are fully covered.  Returns the list of new Allocation rows.

    Example:
      Transaction: $80
      Charges: [Charge(amount_due=$40, amount_paid=$0), Charge(amount_due=$40, amount_paid=$0)]
      → Allocation(txn, charge[0], $40) + Allocation(txn, charge[1], $40)
      → Both charges → PAID

    Partial payment example:
      Transaction: $30
      Charges: [Charge(amount_due=$40, amount_paid=$0)]
      → Allocation(txn, charge[0], $30)
      → Charge[0] → PARTIAL
    """
    remaining = transaction.amount
    new_allocations: list[Allocation] = []

    for charge in charges:
        if remaining <= Decimal("0.00"):
            break

        still_owed = charge.amount_remaining
        if still_owed <= Decimal("0.00"):
            continue  # charge already fully paid (shouldn't be here, but safe)

        apply_amount = min(remaining, still_owed)

        alloc = Allocation(
            transaction_id=transaction.id,
            charge_id=charge.id,
            amount=apply_amount,
            confirmed_by_human=False,
        )
        session.add(alloc)
        new_allocations.append(alloc)

        # Update the in-memory allocations list so amount_paid is correct
        charge.allocations.append(alloc)
        _update_charge_status(charge)

        remaining -= apply_amount

    return new_allocations


# ── Main reconcile functions ───────────────────────────────────────────────────

def reconcile_transaction(
    session: Session,
    transaction: Transaction,
    billing_period: date,
) -> ReconcileResult:
    """Reconcile a single transaction: find its family, allocate to charges.

    Parameters
    ----------
    session : Session
        Open SQLAlchemy session.
    transaction : Transaction
        The transaction ORM object to reconcile.
    billing_period : date
        First day of the month we're reconciling (e.g. date(2026,1,1)).

    Returns
    -------
    ReconcileResult
        Details of what happened.
    """
    result = ReconcileResult(
        transaction_id=transaction.id,
        external_id=transaction.external_id,
    )

    # ── Already reconciled? ───────────────────────────────────────────────
    # Load allocations for this transaction
    existing_allocs = session.scalars(
        select(Allocation).filter_by(transaction_id=transaction.id)
    ).all()
    if existing_allocs:
        result.outcome = "skipped"
        result.notes.append(
            f"Already has {len(existing_allocs)} allocation(s) — not re-processing."
        )
        return result

    # ── Step 1: Find the family ───────────────────────────────────────────
    family: Family | None = None

    # If the transaction already has a family_id set (e.g. from a prior partial run)
    if transaction.family_id is not None:
        family = session.get(Family, transaction.family_id)

    # Otherwise, try name matching
    if family is None and transaction.payer_name:
        family = find_family_by_payer_name(session, transaction.payer_name)

    if family is None:
        result.outcome = "unmatched"
        result.notes.append(
            f"Could not match payer '{transaction.payer_name}' to any family."
        )
        return result

    # Link the transaction to the family we found
    transaction.family_id = family.id
    result.matched_family_id = family.id
    result.notes.append(f"Matched to family '{family.display_name}' (id={family.id}).")

    # ── Step 2: Find open charges for this family ─────────────────────────
    charges = _open_charges_for_family(session, family.id, billing_period)

    if not charges:
        result.outcome = "no_charges"
        result.notes.append(
            f"Family found but no PENDING/PARTIAL charges for period {billing_period}."
        )
        return result

    # ── Step 3: Allocate money to charges ─────────────────────────────────
    new_allocs = allocate_to_charges(session, transaction, charges)

    result.allocations_created = len(new_allocs)
    result.charges_updated = [a.charge_id for a in new_allocs]
    result.outcome = "matched"

    # Check if there's leftover money (overpayment or amount > all charges)
    amount_allocated = sum(a.amount for a in new_allocs)
    leftover = transaction.amount - amount_allocated
    if leftover > Decimal("0.00"):
        result.outcome = "partial"
        result.notes.append(
            f"${leftover} of the ${transaction.amount} payment is unallocated "
            f"(no more open charges for this family in {billing_period})."
        )

    return result


def reconcile_all_pending(
    session: Session,
    billing_period: date,
    *,
    source: TransactionSource | None = None,
) -> list[ReconcileResult]:
    """Reconcile all transactions that have no allocations yet.

    Parameters
    ----------
    session : Session
    billing_period : date
        The billing month to reconcile against.
    source : TransactionSource | None
        If given, only process transactions from this source (e.g. PAYPAL only).
        If None, process all sources.

    Returns
    -------
    list[ReconcileResult]
        One result per transaction processed.
    """
    # Find transactions with no allocations yet
    # We use a NOT EXISTS subquery via the ~ operator on has()
    query = (
        select(Transaction)
        .options(selectinload(Transaction.allocations))
        .order_by(Transaction.paid_on, Transaction.id)
    )
    if source is not None:
        query = query.filter(Transaction.source == source)

    all_txns = session.scalars(query).all()

    # Filter to those with no allocations (unreconciled)
    unreconciled = [t for t in all_txns if not t.allocations]

    results = []
    for txn in unreconciled:
        result = reconcile_transaction(session, txn, billing_period)
        results.append(result)

    return results
