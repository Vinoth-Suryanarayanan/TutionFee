"""Foundation shared by every table: the declarative Base and a few helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Enum, MetaData
from sqlalchemy.orm import DeclarativeBase

# Give every constraint a predictable name. Without this, Postgres invents names, and
# migrations that later drop or change a constraint can't refer to it reliably.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Every table class inherits from this. It collects them all in `Base.metadata`."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Current time with an explicit UTC timezone (never a 'naive' datetime)."""
    return datetime.now(UTC)


def str_enum(enum_cls: type[StrEnum]) -> Enum:
    """Store a Python StrEnum as a short text column limited to the enum's values.

    native_enum=False -> VARCHAR + CHECK constraint instead of a Postgres ENUM type,
    which is much easier to change in later migrations.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        values_callable=lambda members: [m.value for m in members],
        length=32,
        validate_strings=True,
    )
