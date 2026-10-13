"""Test fixtures shared across all test files.

pytest reads this file automatically before running any test.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tuition.db.base import Base

# A fresh SQLite in-memory database for every test.
# Why SQLite instead of Postgres? Because:
# - No Docker required to run tests
# - Each test gets a brand-new empty DB with zero setup
# - SQLAlchemy makes both databases look identical to our code
# We'll add Postgres-specific tests separately for things SQLite can't do.
TEST_URL = "sqlite://"


@pytest.fixture()
def engine():
    """Create all tables in a fresh in-memory SQLite database."""
    e = create_engine(TEST_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)
    e.dispose()


@pytest.fixture()
def session(engine) -> Session:
    """A database session that rolls back after every test (nothing persists)."""
    with Session(engine) as s:
        yield s
        s.rollback()
