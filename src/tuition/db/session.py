"""Connecting to the database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tuition.config import get_settings


def make_engine(url: str | None = None) -> Engine:
    """An Engine manages a pool of connections to one database."""
    return create_engine(url or get_settings().database_url)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """A unit of work: everything inside the `with` block is saved together, or not at all.

        with session_scope() as session:
            session.add(family)
        # committed here — or rolled back if anything raised
    """
    factory = sessionmaker(bind=engine or make_engine(), expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
