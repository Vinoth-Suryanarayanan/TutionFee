"""
tests/test_billing.py
---------------------
Tests for src/tuition/billing.py — the run_billing() function.

What we're testing:
  1. Basic charge creation — active enrollments get exactly one Charge.
  2. Idempotency — calling run_billing() twice for the same period is safe.
  3. Withdrawn enrollments — end_date before billing_period → no charge.
  4. Not-yet-started enrollments — start_date after billing_period → no charge.
  5. Amount copied correctly — charge.amount_due == enrollment.monthly_fee.
  6. family_id denormalised — charge.family_id == student.family_id.
  7. Multi-program family — two enrollments → two charges, both for same family.
  8. Edge: enrollment starts exactly on billing_period day → IS included.
  9. Edge: enrollment end_date == billing_period → IS still included (last month).

Test database:
  Each test gets its own fresh in-memory SQLite database via the `db_session`
  fixture so tests never interfere with each other.  We build only the rows
  each test needs rather than loading the full seed data.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tuition.core.billing import run_billing
from tuition.db.models import (
    Base,
    Charge,
    ChargeStatus,
    Enrollment,
    Family,
    Student,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_session():
    """
    Yield a fresh in-memory SQLite Session for each test.

    SQLite's in-memory databases are isolated per connection, so each test
    starts with empty tables.  We create the schema via Base.metadata.create_all
    (same as the seed script) rather than running Alembic here.
    """
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_family(session: Session, *, key: str = "FAM001", name: str = "Test Family") -> Family:
    """Insert and flush a minimal Family row."""
    family = Family(external_key=key, display_name=name)
    session.add(family)
    session.flush()
    return family


def make_student(
    session: Session,
    family: Family,
    *,
    key: str = "STU001",
    first: str = "Alice",
    last: str = "Smith",
) -> Student:
    """Insert and flush a minimal Student row linked to a Family."""
    student = Student(
        family_id=family.id,
        external_key=key,
        first_name=first,
        last_name=last,
    )
    session.add(student)
    session.flush()
    return student


def make_enrollment(
    session: Session,
    student: Student,
    *,
    program: str = "Carnatic",
    monthly_fee: Decimal = Decimal("40.00"),
    start_date: date = date(2025, 9, 1),
    end_date: date | None = None,
) -> Enrollment:
    """Insert and flush an Enrollment row."""
    enrollment = Enrollment(
        student_id=student.id,
        program=program,
        monthly_fee=monthly_fee,
        start_date=start_date,
        end_date=end_date,
    )
    session.add(enrollment)
    session.flush()
    return enrollment


# ── Tests ─────────────────────────────────────────────────────────────────────

BILLING_JAN = date(2026, 1, 1)


class TestBasicChargeCreation:
    """Happy-path: active enrollment → one charge created."""

    def test_creates_charge_for_active_enrollment(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(db_session, student)

        new_charges = run_billing(db_session, BILLING_JAN)

        assert len(new_charges) == 1

    def test_charge_status_is_pending(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(db_session, student)

        [charge] = run_billing(db_session, BILLING_JAN)

        assert charge.status == ChargeStatus.PENDING

    def test_no_enrollments_returns_empty_list(self, db_session):
        result = run_billing(db_session, BILLING_JAN)
        assert result == []


class TestAmountAndFamilyId:
    """Verify the charge copies amount_due and family_id correctly."""

    def test_amount_due_equals_monthly_fee(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        enrollment = make_enrollment(db_session, student, monthly_fee=Decimal("40.00"))

        [charge] = run_billing(db_session, BILLING_JAN)

        assert charge.amount_due == enrollment.monthly_fee
        assert charge.amount_due == Decimal("40.00")

    def test_family_id_denormalised_correctly(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(db_session, student)

        [charge] = run_billing(db_session, BILLING_JAN)

        assert charge.family_id == family.id

    def test_billing_period_stored_on_charge(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(db_session, student)

        [charge] = run_billing(db_session, BILLING_JAN)

        assert charge.billing_period == BILLING_JAN


class TestIdempotency:
    """run_billing() twice for the same period must not create duplicate charges."""

    def test_second_call_returns_empty_list(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(db_session, student)

        first = run_billing(db_session, BILLING_JAN)
        second = run_billing(db_session, BILLING_JAN)

        assert len(first) == 1
        assert second == []

    def test_only_one_charge_row_in_db_after_two_calls(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(db_session, student)

        run_billing(db_session, BILLING_JAN)
        run_billing(db_session, BILLING_JAN)

        from sqlalchemy import select
        all_charges = db_session.scalars(select(Charge)).all()
        assert len(all_charges) == 1

    def test_different_periods_both_create_charges(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(db_session, student)

        jan = run_billing(db_session, date(2026, 1, 1))
        feb = run_billing(db_session, date(2026, 2, 1))

        assert len(jan) == 1
        assert len(feb) == 1


class TestEnrollmentActivation:
    """Only enrollments active during billing_period should be charged."""

    def test_withdrawn_enrollment_not_charged(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(
            db_session, student,
            start_date=date(2025, 9, 1),
            end_date=date(2025, 12, 31),
        )

        result = run_billing(db_session, BILLING_JAN)

        assert result == []

    def test_enrollment_ending_on_billing_period_is_charged(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(
            db_session, student,
            start_date=date(2025, 9, 1),
            end_date=date(2026, 1, 1),
        )

        result = run_billing(db_session, BILLING_JAN)

        assert len(result) == 1

    def test_future_enrollment_not_charged(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(
            db_session, student,
            start_date=date(2026, 2, 1),
        )

        result = run_billing(db_session, BILLING_JAN)

        assert result == []

    def test_enrollment_starting_on_billing_period_is_charged(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(
            db_session, student,
            start_date=date(2026, 1, 1),
        )

        result = run_billing(db_session, BILLING_JAN)

        assert len(result) == 1


class TestMultipleEnrollments:
    """Families with multiple programs or students get all their charges."""

    def test_two_programs_same_student_two_charges(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family, key="STU004", first="Ethan")
        make_enrollment(db_session, student, program="Carnatic")
        make_enrollment(db_session, student, program="Yoga")

        charges = run_billing(db_session, BILLING_JAN)

        assert len(charges) == 2
        total = sum(c.amount_due for c in charges)
        assert total == Decimal("80.00")

    def test_two_students_same_family_two_charges(self, db_session):
        family = make_family(db_session, key="FAM002", name="Patel Family")
        arjun = make_student(db_session, family, key="STU002", first="Arjun")
        meera = make_student(db_session, family, key="STU003", first="Meera")
        make_enrollment(db_session, arjun, program="Carnatic")
        make_enrollment(db_session, meera, program="Yoga")

        charges = run_billing(db_session, BILLING_JAN)

        assert len(charges) == 2
        assert all(c.family_id == family.id for c in charges)

    def test_two_families_independent_charges(self, db_session):
        fam1 = make_family(db_session, key="FAM001", name="Smith Family")
        fam2 = make_family(db_session, key="FAM002", name="Jones Family")
        alice = make_student(db_session, fam1, key="STU001")
        bob = make_student(db_session, fam2, key="STU002", first="Bob")
        make_enrollment(db_session, alice, program="Carnatic")
        make_enrollment(db_session, bob, program="Yoga")

        charges = run_billing(db_session, BILLING_JAN)

        assert len(charges) == 2
        family_ids = {c.family_id for c in charges}
        assert family_ids == {fam1.id, fam2.id}

    def test_mixed_active_and_withdrawn(self, db_session):
        family = make_family(db_session)
        student = make_student(db_session, family)
        make_enrollment(db_session, student, program="Carnatic")
        make_enrollment(
            db_session, student, program="Yoga",
            end_date=date(2025, 12, 31),
        )

        charges = run_billing(db_session, BILLING_JAN)

        assert len(charges) == 1
        assert charges[0].enrollment_id is not None
