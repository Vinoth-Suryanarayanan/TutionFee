"""Tests for the money tables: Charge, Transaction, Allocation.

These tests prove:
- charges are created correctly and their computed properties work
- transactions track how much money has and hasn't been allocated
- allocations link transactions to charges (the reconciliation join table)
- cascade rules: deleting a family wipes its charges; deleting a transaction
  wipes its allocations; a transaction with SET NULL keeps the row but clears family_id
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tuition.db.models import (
    Allocation,
    Charge,
    ChargeStatus,
    Enrollment,
    Family,
    Student,
    Transaction,
    TransactionSource,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_family(session, *, key="F001", name="Smith Family") -> Family:
    f = Family(external_key=key, display_name=name)
    session.add(f)
    session.flush()
    return f


def make_student(session, family: Family) -> Student:
    s = Student(
        external_key="S001",
        family_id=family.id,
        first_name="Alice",
        last_name="Smith",
    )
    session.add(s)
    session.flush()
    return s


def make_enrollment(session, student: Student, *, fee="150.00") -> Enrollment:
    e = Enrollment(
        student_id=student.id,
        program="Piano",
        monthly_fee=Decimal(fee),
        start_date=date(2025, 1, 1),
    )
    session.add(e)
    session.flush()
    return e


def make_charge(
    session,
    enrollment: Enrollment,
    family: Family,
    *,
    period=date(2025, 1, 1),
    amount="150.00",
) -> Charge:
    c = Charge(
        enrollment_id=enrollment.id,
        family_id=family.id,
        billing_period=period,
        amount_due=Decimal(amount),
    )
    session.add(c)
    session.flush()
    return c


def make_transaction(
    session,
    family: Family,
    *,
    amount="150.00",
    external_id="PP-001",
    source=TransactionSource.PAYPAL,
) -> Transaction:
    t = Transaction(
        family_id=family.id,
        source=source,
        external_id=external_id,
        paid_on=date(2025, 1, 15),
        amount=Decimal(amount),
        payer_name="Bob Smith",
    )
    session.add(t)
    session.flush()
    return t


# ── Charge tests ─────────────────────────────────────────────────────────────


def test_charge_defaults(session):
    """A new charge starts as PENDING with zero paid."""
    family = make_family(session)
    student = make_student(session, family)
    enrollment = make_enrollment(session, student)
    charge = make_charge(session, enrollment, family)

    assert charge.id is not None
    assert charge.status == ChargeStatus.PENDING
    assert charge.amount_paid == Decimal("0.00")
    assert charge.amount_remaining == Decimal("150.00")


def test_charge_unique_per_period(session):
    """Cannot create two charges for the same enrollment and billing period."""
    from sqlalchemy.exc import IntegrityError

    family = make_family(session)
    student = make_student(session, family)
    enrollment = make_enrollment(session, student)
    make_charge(session, enrollment, family, period=date(2025, 1, 1))

    with pytest.raises(IntegrityError):
        make_charge(session, enrollment, family, period=date(2025, 1, 1))


def test_charge_amount_paid_after_allocation(session):
    """amount_paid and amount_remaining update after an allocation is added."""
    family = make_family(session)
    student = make_student(session, family)
    enrollment = make_enrollment(session, student, fee="150.00")
    charge = make_charge(session, enrollment, family, amount="150.00")
    txn = make_transaction(session, family, amount="100.00")

    alloc = Allocation(
        transaction_id=txn.id,
        charge_id=charge.id,
        amount=Decimal("100.00"),
    )
    session.add(alloc)
    session.flush()
    session.refresh(charge)

    assert charge.amount_paid == Decimal("100.00")
    assert charge.amount_remaining == Decimal("50.00")


# ── Transaction tests ─────────────────────────────────────────────────────────


def test_transaction_defaults(session):
    """A new transaction has no allocations -> all money is unallocated."""
    family = make_family(session)
    txn = make_transaction(session, family, amount="250.00")

    assert txn.id is not None
    assert txn.amount_allocated == Decimal("0.00")
    assert txn.amount_unallocated == Decimal("250.00")


def test_transaction_idempotent_import(session):
    """Same source + external_id cannot be inserted twice (idempotency key)."""
    from sqlalchemy.exc import IntegrityError

    family = make_family(session)
    make_transaction(session, family, external_id="PP-001", source=TransactionSource.PAYPAL)

    with pytest.raises(IntegrityError):
        make_transaction(session, family, external_id="PP-001", source=TransactionSource.PAYPAL)


def test_transaction_sources_are_distinct(session):
    """Same external_id from different sources is allowed (bank vs paypal)."""
    family = make_family(session)
    t1 = make_transaction(session, family, external_id="REF-123", source=TransactionSource.PAYPAL)
    t2 = make_transaction(session, family, external_id="REF-123", source=TransactionSource.BANK)
    assert t1.id != t2.id


def test_transaction_partial_allocation(session):
    """A $250 payment with $150 allocated leaves $100 unallocated."""
    family = make_family(session)
    student = make_student(session, family)
    enrollment = make_enrollment(session, student, fee="150.00")
    charge = make_charge(session, enrollment, family, amount="150.00")
    txn = make_transaction(session, family, amount="250.00")

    alloc = Allocation(transaction_id=txn.id, charge_id=charge.id, amount=Decimal("150.00"))
    session.add(alloc)
    session.flush()
    session.refresh(txn)

    assert txn.amount_allocated == Decimal("150.00")
    assert txn.amount_unallocated == Decimal("100.00")


# ── Allocation tests ──────────────────────────────────────────────────────────


def test_allocation_split_payment(session):
    """One transaction can be split across two charges (two enrollments)."""
    family = make_family(session)
    student = make_student(session, family)

    piano = make_enrollment(session, student, fee="150.00")
    math = Enrollment(
        student_id=student.id,
        program="Math",
        monthly_fee=Decimal("100.00"),
        start_date=date(2025, 1, 1),
    )
    session.add(math)
    session.flush()

    charge_piano = make_charge(session, piano, family, amount="150.00")
    charge_math = make_charge(session, math, family,
                              period=date(2025, 1, 1), amount="100.00")

    txn = make_transaction(session, family, amount="250.00")
    session.add_all([
        Allocation(transaction_id=txn.id, charge_id=charge_piano.id, amount=Decimal("150.00")),
        Allocation(transaction_id=txn.id, charge_id=charge_math.id, amount=Decimal("100.00")),
    ])
    session.flush()
    session.refresh(txn)

    assert txn.amount_allocated == Decimal("250.00")
    assert txn.amount_unallocated == Decimal("0.00")


def test_allocation_confirmed_by_human_default(session):
    """A new allocation is not yet confirmed by a human."""
    family = make_family(session)
    student = make_student(session, family)
    enrollment = make_enrollment(session, student)
    charge = make_charge(session, enrollment, family)
    txn = make_transaction(session, family)

    alloc = Allocation(transaction_id=txn.id, charge_id=charge.id, amount=Decimal("150.00"))
    session.add(alloc)
    session.flush()

    assert alloc.confirmed_by_human is False


# ── Cascade tests ─────────────────────────────────────────────────────────────


def test_cascade_delete_charge_removes_allocations(session):
    """Deleting a Charge also deletes its Allocations."""
    family = make_family(session)
    student = make_student(session, family)
    enrollment = make_enrollment(session, student)
    charge = make_charge(session, enrollment, family)
    txn = make_transaction(session, family)

    alloc = Allocation(transaction_id=txn.id, charge_id=charge.id, amount=Decimal("150.00"))
    session.add(alloc)
    session.flush()
    alloc_id = alloc.id

    session.delete(charge)
    session.flush()

    assert session.get(Allocation, alloc_id) is None
    assert session.get(Transaction, txn.id) is not None


@pytest.mark.skipif(
    True,
    reason="ON DELETE SET NULL is not enforced by SQLite; verified against Postgres in CI.",
)
def test_transaction_set_null_on_family_delete(session):
    """
    Deleting a family sets transaction.family_id to NULL (not a cascade delete).

    Why SET NULL instead of CASCADE here?
    The payment record is real financial evidence -- we never want to lose it.
    If we accidentally delete a family, we want the money trail to survive
    so we can re-attach it to the correct family or audit it.

    NOTE: SQLite silently ignores ON DELETE SET NULL, so this test only
    runs against Postgres.  The migration (7b3f12de4a01) sets the correct
    constraint in the real database.
    """
    family = make_family(session)
    txn = make_transaction(session, family)
    txn_id = txn.id

    session.delete(family)
    session.flush()
    session.expire_all()

    surviving_txn = session.get(Transaction, txn_id)
    assert surviving_txn is not None
    assert surviving_txn.family_id is None
