"""
tests/test_reconciler.py
-------------------------
Tests for src/tuition/reconciler.py.

What we're testing:
  find_family_by_payer_name():
    1.  Exact guardian name match → returns family
    2.  Case-insensitive guardian name match → returns family
    3.  Display name match (bank-style "Smith Family") → returns family
    4.  Unknown name → None
    5.  Ambiguous (same name, two families) → None

  allocate_to_charges():
    6.  Full payment covers one charge exactly → PAID
    7.  Overpayment: more money than one charge → first charge PAID, leftover unallocated
    8.  Multi-charge: payment covers two charges exactly → both PAID
    9.  Partial payment: not enough for one charge → PARTIAL
    10. Combined partial: two transactions together cover one charge

  reconcile_transaction():
    11. Happy path: name matches, charge exists → matched + allocation created
    12. Unknown payer → unmatched
    13. Family found, no charges for period → no_charges
    14. Already has allocations → skipped (idempotent)
    15. Partial payment leaves charge as PARTIAL
    16. transaction.family_id already set → uses it, skips name matching

  reconcile_all_pending():
    17. Reconciles all unallocated transactions
    18. Already-allocated transactions are skipped
    19. Source filter works (only PAYPAL or only BANK)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tuition.db.models import (
    Allocation,
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
from tuition.core.reconciler import (
    ReconcileResult,
    allocate_to_charges,
    find_family_by_payer_name,
    reconcile_all_pending,
    reconcile_transaction,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────

BILLING_JAN = date(2026, 1, 1)


def make_family(session, *, key="FAM001", name="Smith Family") -> Family:
    f = Family(external_key=key, display_name=name)
    session.add(f)
    session.flush()
    return f


def make_guardian(session, family, *, full_name="Robert Smith") -> Guardian:
    g = Guardian(
        family_id=family.id,
        full_name=full_name,
        email=f"{full_name.lower().replace(' ','.')}@test.com",
        is_primary=True,
        preferred_channel="email",
    )
    session.add(g)
    session.flush()
    return g


def make_student(session, family, *, key="STU001") -> Student:
    s = Student(family_id=family.id, external_key=key, first_name="Alice", last_name="Smith")
    session.add(s)
    session.flush()
    return s


def make_enrollment(session, student, *, program="Carnatic", fee="40.00") -> Enrollment:
    e = Enrollment(
        student_id=student.id,
        program=program,
        monthly_fee=Decimal(fee),
        start_date=date(2025, 9, 1),
    )
    session.add(e)
    session.flush()
    return e


def make_charge(
    session,
    family: Family,
    enrollment: Enrollment,
    *,
    amount="40.00",
    period=BILLING_JAN,
    status=ChargeStatus.PENDING,
) -> Charge:
    c = Charge(
        enrollment_id=enrollment.id,
        family_id=family.id,
        billing_period=period,
        amount_due=Decimal(amount),
        status=status,
    )
    session.add(c)
    session.flush()
    return c


def make_transaction(
    session,
    *,
    source=TransactionSource.PAYPAL,
    ext_id="PP-T001",
    amount="40.00",
    payer_name="Robert Smith",
    family_id=None,
) -> Transaction:
    t = Transaction(
        source=source,
        external_id=ext_id,
        paid_on=date(2026, 1, 5),
        amount=Decimal(amount),
        payer_name=payer_name,
        family_id=family_id,
    )
    session.add(t)
    session.flush()
    return t


# ── Tests: find_family_by_payer_name ─────────────────────────────────────────

class TestFindFamilyByPayerName:
    def test_exact_guardian_name_match(self, db_session):
        family = make_family(db_session)
        make_guardian(db_session, family, full_name="Robert Smith")

        found = find_family_by_payer_name(db_session, "Robert Smith")

        assert found is not None
        assert found.id == family.id

    def test_case_insensitive_guardian_match(self, db_session):
        family = make_family(db_session)
        make_guardian(db_session, family, full_name="Robert Smith")

        found = find_family_by_payer_name(db_session, "robert smith")

        assert found is not None
        assert found.id == family.id

    def test_family_display_name_match(self, db_session):
        """Bank descriptions often look like 'SMITH FAMILY VRUKSHAA' — we try display_name too."""
        family = make_family(db_session, name="smith family")
        # no guardian added — only family display_name

        found = find_family_by_payer_name(db_session, "smith family")

        assert found is not None
        assert found.id == family.id

    def test_unknown_payer_returns_none(self, db_session):
        make_family(db_session)

        found = find_family_by_payer_name(db_session, "Complete Stranger")

        assert found is None

    def test_ambiguous_payer_returns_none(self, db_session):
        """Two families share a guardian name — we can't decide, return None."""
        fam1 = make_family(db_session, key="FAM001", name="Family One")
        fam2 = make_family(db_session, key="FAM002", name="Family Two")
        make_guardian(db_session, fam1, full_name="John Doe")
        make_guardian(db_session, fam2, full_name="John Doe")

        found = find_family_by_payer_name(db_session, "John Doe")

        assert found is None

    def test_empty_name_returns_none(self, db_session):
        make_family(db_session)
        assert find_family_by_payer_name(db_session, "") is None


# ── Tests: allocate_to_charges ────────────────────────────────────────────────

class TestAllocateToCharges:
    def _setup(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        enrollment = make_enrollment(db_session, student)
        return family, student, enrollment

    def test_exact_payment_marks_charge_paid(self, db_session):
        family, student, enrollment = self._setup(db_session)
        charge = make_charge(db_session, family, enrollment, amount="40.00")
        txn = make_transaction(db_session, amount="40.00")

        allocs = allocate_to_charges(db_session, txn, [charge])

        assert len(allocs) == 1
        assert allocs[0].amount == Decimal("40.00")
        assert charge.status == ChargeStatus.PAID

    def test_partial_payment_marks_charge_partial(self, db_session):
        family, student, enrollment = self._setup(db_session)
        charge = make_charge(db_session, family, enrollment, amount="40.00")
        txn = make_transaction(db_session, amount="30.00")

        allocs = allocate_to_charges(db_session, txn, [charge])

        assert len(allocs) == 1
        assert allocs[0].amount == Decimal("30.00")
        assert charge.status == ChargeStatus.PARTIAL

    def test_payment_covers_two_charges(self, db_session):
        """$80 payment covers two $40 charges (e.g. Carnatic + Yoga)."""
        family, student, enrollment = self._setup(db_session)
        student2 = make_student(db_session, family, key="STU002")
        enrollment2 = make_enrollment(db_session, student2, program="Yoga")

        charge1 = make_charge(db_session, family, enrollment, amount="40.00")
        charge2 = make_charge(db_session, family, enrollment2, amount="40.00")
        txn = make_transaction(db_session, amount="80.00")

        allocs = allocate_to_charges(db_session, txn, [charge1, charge2])

        assert len(allocs) == 2
        assert charge1.status == ChargeStatus.PAID
        assert charge2.status == ChargeStatus.PAID

    def test_overpayment_leaves_second_charge_with_remaining(self, db_session):
        """$50 against one $40 charge — $10 unallocated (no more charges)."""
        family, student, enrollment = self._setup(db_session)
        charge = make_charge(db_session, family, enrollment, amount="40.00")
        txn = make_transaction(db_session, amount="50.00")

        allocs = allocate_to_charges(db_session, txn, [charge])

        assert len(allocs) == 1
        assert allocs[0].amount == Decimal("40.00")  # only what the charge needed
        assert charge.status == ChargeStatus.PAID

    def test_no_charges_returns_empty(self, db_session):
        txn = make_transaction(db_session)
        allocs = allocate_to_charges(db_session, txn, [])
        assert allocs == []


# ── Tests: reconcile_transaction ─────────────────────────────────────────────

class TestReconcileTransaction:
    def _full_setup(self, db_session, *, payer_name="Robert Smith", amount="40.00"):
        family = make_family(db_session)
        make_guardian(db_session, family, full_name=payer_name)
        student = make_student(db_session, family)
        enrollment = make_enrollment(db_session, student)
        charge = make_charge(db_session, family, enrollment)
        txn = make_transaction(db_session, amount=amount, payer_name=payer_name)
        return family, charge, txn

    def test_happy_path_creates_allocation(self, db_session):
        family, charge, txn = self._full_setup(db_session)

        result = reconcile_transaction(db_session, txn, BILLING_JAN)

        assert result.outcome == "matched"
        assert result.matched_family_id == family.id
        assert result.allocations_created == 1
        assert charge.status == ChargeStatus.PAID

    def test_unknown_payer_returns_unmatched(self, db_session):
        txn = make_transaction(db_session, payer_name="Nobody Here")

        result = reconcile_transaction(db_session, txn, BILLING_JAN)

        assert result.outcome == "unmatched"
        assert result.matched_family_id is None

    def test_family_found_no_charges_returns_no_charges(self, db_session):
        family = make_family(db_session)
        make_guardian(db_session, family, full_name="Known Person")
        txn = make_transaction(db_session, payer_name="Known Person")
        # No charges created for this family

        result = reconcile_transaction(db_session, txn, BILLING_JAN)

        assert result.outcome == "no_charges"

    def test_already_allocated_returns_skipped(self, db_session):
        family, charge, txn = self._full_setup(db_session)
        # Manually add an allocation
        alloc = Allocation(
            transaction_id=txn.id,
            charge_id=charge.id,
            amount=Decimal("40.00"),
        )
        db_session.add(alloc)
        db_session.flush()

        result = reconcile_transaction(db_session, txn, BILLING_JAN)

        assert result.outcome == "skipped"

    def test_partial_payment_outcome(self, db_session):
        family, charge, txn = self._full_setup(db_session, amount="30.00")

        result = reconcile_transaction(db_session, txn, BILLING_JAN)

        assert result.outcome == "matched"  # matched, but charge is PARTIAL
        assert charge.status == ChargeStatus.PARTIAL

    def test_payment_exceeds_all_charges_returns_partial_outcome(self, db_session):
        """$100 against a $40 charge — surplus, outcome='partial'."""
        family, charge, txn = self._full_setup(db_session, amount="100.00")

        result = reconcile_transaction(db_session, txn, BILLING_JAN)

        assert result.outcome == "partial"
        assert charge.status == ChargeStatus.PAID  # charge IS paid
        assert any("unallocated" in note for note in result.notes)

    def test_family_id_already_set_on_transaction(self, db_session):
        """If transaction.family_id is already set, skip name matching."""
        family = make_family(db_session)
        # No guardian — name matching would fail
        student = make_student(db_session, family)
        enrollment = make_enrollment(db_session, student)
        make_charge(db_session, family, enrollment)
        txn = make_transaction(
            db_session,
            payer_name="Completely Unknown Name",
            family_id=family.id,  # pre-set
        )

        result = reconcile_transaction(db_session, txn, BILLING_JAN)

        assert result.outcome == "matched"
        assert result.matched_family_id == family.id

    def test_wrong_billing_period_returns_no_charges(self, db_session):
        """Charges exist for Jan but we reconcile for Feb — no match."""
        family, charge, txn = self._full_setup(db_session)

        result = reconcile_transaction(db_session, txn, date(2026, 2, 1))

        assert result.outcome == "no_charges"


# ── Tests: reconcile_all_pending ──────────────────────────────────────────────

class TestReconcileAllPending:
    def test_processes_all_unallocated(self, db_session):
        fam1 = make_family(db_session, key="FAM001", name="Smith Family")
        fam2 = make_family(db_session, key="FAM002", name="Jones Family")
        make_guardian(db_session, fam1, full_name="Robert Smith")
        make_guardian(db_session, fam2, full_name="Mary Jones")
        s1 = make_student(db_session, fam1, key="STU001")
        s2 = make_student(db_session, fam2, key="STU002")
        e1 = make_enrollment(db_session, s1)
        e2 = make_enrollment(db_session, s2, program="Yoga")
        make_charge(db_session, fam1, e1)
        make_charge(db_session, fam2, e2)
        make_transaction(db_session, ext_id="PP-T001", payer_name="Robert Smith")
        make_transaction(db_session, ext_id="PP-T002", payer_name="Mary Jones")

        results = reconcile_all_pending(db_session, BILLING_JAN)

        assert len(results) == 2
        assert all(r.outcome == "matched" for r in results)

    def test_already_allocated_transactions_skipped(self, db_session):
        family = make_family(db_session)
        make_guardian(db_session, family, full_name="Robert Smith")
        student = make_student(db_session, family)
        enrollment = make_enrollment(db_session, student)
        charge = make_charge(db_session, family, enrollment)
        txn = make_transaction(db_session)

        # First run
        reconcile_all_pending(db_session, BILLING_JAN)
        db_session.flush()

        # Second run — txn already has allocations
        results = reconcile_all_pending(db_session, BILLING_JAN)

        assert all(r.outcome == "skipped" for r in results)

    def test_source_filter_only_processes_paypal(self, db_session):
        family = make_family(db_session)
        make_guardian(db_session, family, full_name="Robert Smith")
        student = make_student(db_session, family)
        e1 = make_enrollment(db_session, student)
        e2 = make_enrollment(db_session, student, program="Yoga")
        make_charge(db_session, family, e1)
        make_charge(db_session, family, e2)

        make_transaction(db_session, ext_id="PP-SRC01", source=TransactionSource.PAYPAL)
        make_transaction(db_session, ext_id="BNK-SRC01", source=TransactionSource.BANK)

        results = reconcile_all_pending(
            db_session, BILLING_JAN, source=TransactionSource.PAYPAL
        )

        # Only the PayPal transaction was processed
        assert len(results) == 1
        assert results[0].external_id == "PP-SRC01"
