"""Tests for the roster tables: Family, Guardian, Student, Enrollment.

Each test gets a fresh empty database (from the session fixture in conftest.py).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tuition.db.models import Channel, Enrollment, Family, Guardian, Student


# ── Helpers ─────────────────────────────────────────────────────────────────

def make_family(session, *, external_key="F001", display_name="Smith Family") -> Family:
    f = Family(external_key=external_key, display_name=display_name)
    session.add(f)
    session.flush()  # assigns f.id without committing
    return f


def make_student(session, family: Family, *, first="Alice", last="Smith") -> Student:
    s = Student(
        external_key=f"S-{first[:2].upper()}01",
        family_id=family.id,
        first_name=first,
        last_name=last,
    )
    session.add(s)
    session.flush()
    return s


# ── Tests ────────────────────────────────────────────────────────────────────

def test_create_family(session):
    """A family can be saved and retrieved."""
    f = make_family(session)
    assert f.id is not None
    retrieved = session.get(Family, f.id)
    assert retrieved.display_name == "Smith Family"


def test_guardian_default_channel(session):
    """A guardian's preferred_channel defaults to email."""
    family = make_family(session)
    g = Guardian(family_id=family.id, full_name="Bob Smith")
    session.add(g)
    session.flush()
    assert g.preferred_channel == Channel.EMAIL


def test_guardian_relationship(session):
    """Family.guardians returns the guardians we added."""
    family = make_family(session)
    g1 = Guardian(family_id=family.id, full_name="Bob Smith", is_primary=True)
    g2 = Guardian(family_id=family.id, full_name="Carol Smith")
    session.add_all([g1, g2])
    session.flush()
    session.refresh(family)
    assert len(family.guardians) == 2
    primaries = [g for g in family.guardians if g.is_primary]
    assert len(primaries) == 1


def test_student_relationship(session):
    """Family.students returns the students we added."""
    family = make_family(session)
    alice = make_student(session, family, first="Alice")
    bob = make_student(session, family, first="Bob", last="Smith")
    session.refresh(family)
    assert len(family.students) == 2


def test_enrollment_is_active_on(session):
    """Enrollment.is_active_on() respects start and end dates."""
    family = make_family(session)
    student = make_student(session, family)
    enroll = Enrollment(
        student_id=student.id,
        program="Piano",
        monthly_fee=Decimal("150.00"),
        start_date=date(2025, 1, 1),
        end_date=date(2025, 6, 30),
    )
    session.add(enroll)
    session.flush()

    assert enroll.is_active_on(date(2025, 1, 1))   # first day
    assert enroll.is_active_on(date(2025, 3, 15))  # middle
    assert enroll.is_active_on(date(2025, 6, 30))  # last day
    assert not enroll.is_active_on(date(2024, 12, 31))  # before
    assert not enroll.is_active_on(date(2025, 7, 1))    # after


def test_enrollment_open_ended_is_active(session):
    """An enrollment with no end_date is still active."""
    family = make_family(session)
    student = make_student(session, family)
    enroll = Enrollment(
        student_id=student.id,
        program="Math",
        monthly_fee=Decimal("100.00"),
        start_date=date(2024, 9, 1),
        end_date=None,  # still enrolled
    )
    session.add(enroll)
    session.flush()

    assert enroll.is_active_on(date(2025, 12, 31))  # far future
    assert enroll.is_active_on(date(2024, 9, 1))    # first day


def test_cascade_delete_family(session):
    """Deleting a family also deletes its guardians and students."""
    family = make_family(session)
    student = make_student(session, family)
    guardian = Guardian(family_id=family.id, full_name="Parent")
    session.add(guardian)
    session.flush()

    family_id = family.id
    student_id = student.id
    guardian_id = guardian.id

    session.delete(family)
    session.flush()

    assert session.get(Family, family_id) is None
    assert session.get(Student, student_id) is None
    assert session.get(Guardian, guardian_id) is None
