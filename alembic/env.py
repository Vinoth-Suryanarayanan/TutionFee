"""Alembic environment: connects our models to the migration engine.

Two things we added vs. the generated default:
1. Import our Base so Alembic can compare models to the DB (autogenerate).
2. Read DATABASE_URL from .env via our Settings object, not from alembic.ini.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# ── 1. Wire up our models ────────────────────────────────────────────────────
# Import the models module so all table classes register on Base.metadata.
# If we skip this, Alembic sees an empty schema and generates no migration.
import tuition.db.models as _models  # noqa: F401 — side-effect import
from tuition.db.base import Base

target_metadata = Base.metadata

# ── 2. Read the database URL from our .env / environment ────────────────────
from tuition.config import get_settings

# Override alembic.ini's placeholder with the real URL from our settings.
context.config.set_main_option("sqlalchemy.url", get_settings().database_url)

# ── Standard boilerplate (unchanged from generated) ─────────────────────────
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — useful for reviewing changes."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the database and apply migrations."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
