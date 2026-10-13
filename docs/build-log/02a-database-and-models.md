# Step 2a — Postgres, SQLAlchemy models, and the first migration

After Step 1 we had a typed config, but nowhere to store data. Step 2a adds:

1. **Docker Compose** — one command to start a local Postgres database
2. **SQLAlchemy models** — Python classes that map to database tables
3. **Alembic migration** — the SQL that creates those tables, version-controlled
4. **Tests** — 7 tests that run without Docker using SQLite in-memory

---

## New files in this step

```
docker-compose.yml           ← Postgres container definition
alembic.ini                  ← Alembic config (URL overridden at runtime)
alembic/
  env.py                     ← wires our models to Alembic
  script.py.mako             ← template for new migration files
  versions/
    4a9abceb93c7_create_roster_tables.py  ← the generated migration SQL
src/tuition/db/
  __init__.py                ← makes db/ a package
  base.py                    ← Base class + helpers shared by all tables
  models.py                  ← the 4 roster tables
  session.py                 ← connect to DB, session_scope context manager
tests/
  conftest.py                ← shared fixtures (in-memory SQLite engine)
  test_roster_models.py      ← 7 tests
```

---

## `docker-compose.yml` — one command, one database

```yaml
services:
  db:
    image: postgres:16-alpine
```

`postgres:16-alpine` is a pre-built image from Docker Hub.
`alpine` = a minimal Linux — the image is about 80 MB vs 400 MB for the full one.

```yaml
    environment:
      POSTGRES_USER: tuition
      POSTGRES_PASSWORD: tuition
      POSTGRES_DB: tuition
```

These three env vars are read by the Postgres startup script inside the container.
They match what's in `.env.example` → `DATABASE_URL`.

```yaml
    ports:
      - "5432:5432"
```

Format is `host_port:container_port`. Postgres inside the container listens on 5432.
This line forwards your Mac's port 5432 to the container's port 5432.
So `localhost:5432` on your Mac talks to Postgres in the container.

```yaml
    volumes:
      - tuition-db:/var/lib/postgresql/data
```

Without this, data disappears when the container stops. The named volume
`tuition-db` (defined at the bottom) lives on your Mac's disk and survives restarts.

```yaml
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tuition -d tuition"]
      interval: 5s
      retries: 5
```

Docker checks "is Postgres actually ready to accept connections?" every 5 seconds.
`--wait` in `make up` waits until healthy before returning. Without this, code that
runs right after `make up` might try to connect before Postgres is ready.

**Commands:**
```bash
make up      # docker compose up -d --wait
make down    # stop, keep data
make reset   # wipe data, restart, re-run migrations
```

---

## `src/tuition/db/base.py` — shared foundation

```python
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    ...
}
```

Every database constraint (PRIMARY KEY, UNIQUE, CHECK, FOREIGN KEY) has a name.
Without this dict, Postgres invents names like `fk_12345abc`.
With it, names are predictable: `fk_guardians_family_id_families`.

Why does this matter? When a later migration wants to *drop* or *change* a
constraint, Alembic refers to it by name. Unpredictable names break that.

```python
class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

`DeclarativeBase` is SQLAlchemy's base class for the "declarative" style —
where you write table definitions as Python classes (vs. raw `Table()` objects).
Every table class in `models.py` inherits from this `Base`, which registers it in
`Base.metadata` — the complete schema in Python.

```python
def utcnow() -> datetime:
    return datetime.now(UTC)
```

Python has "naive" datetimes (no timezone) and "aware" datetimes (with timezone).
Naive datetimes cause subtle bugs in daylight-saving time logic. We always use
`datetime.now(UTC)` — an aware datetime in UTC. `UTC` is `timezone.utc` from
`datetime`, imported as `from datetime import UTC`.

```python
def str_enum(enum_cls: type[StrEnum]) -> Enum:
    return Enum(enum_cls, native_enum=False, ...)
```

`Channel.EMAIL` / `Channel.SMS` is stored as the string `"email"` / `"sms"`.
`native_enum=False` → use a VARCHAR + CHECK constraint, not a Postgres `ENUM` type.

Postgres ENUM types are painful to migrate: you can't add a new value in a
transaction. A CHECK constraint is just a regular column — easy to change later.

---

## `src/tuition/db/models.py` — the four roster tables

### How SQLAlchemy 2.0 type annotations work

```python
id: Mapped[int] = mapped_column(primary_key=True)
```

`Mapped[int]` is a Python type annotation. SQLAlchemy 2.0 reads these at class-
creation time to infer the column type and nullability:
- `Mapped[int]` → NOT NULL INTEGER
- `Mapped[str | None]` → nullable VARCHAR
- `Mapped[Decimal]` → NOT NULL (and we specify NUMERIC(10,2) in mapped_column)

This is brand new in SQLAlchemy 2.0. Older code used `Column(Integer, ...)` without
type hints — you'd get no IDE autocomplete and runtime type errors.

### Family

```python
class Family(Base):
    __tablename__ = "families"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_key: Mapped[str] = mapped_column(String(32), unique=True)
```

`external_key` is a string you control (e.g. `"F001"`, `"SMITH-01"`). It's how
you refer to a family in CSV imports and CLI commands without knowing the auto-
generated `id`. `unique=True` → Postgres enforces no two families can share a key.

```python
    guardians: Mapped[list[Guardian]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )
```

This is **not a column**. It's a Python attribute that SQLAlchemy populates by
running a JOIN when you access `family.guardians`. No extra column in the DB.

`cascade="all, delete-orphan"` → when you `session.delete(family)`, SQLAlchemy
automatically deletes all linked guardians and students too. Without this, the
FK constraint would raise an error (or leave orphan rows, if you set `ondelete="SET NULL"`).

### Guardian

```python
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
```

`ForeignKey("families.id")` → this column must contain a valid `families.id` value.
`ondelete="CASCADE"` → if the parent family row is deleted at the DB level (e.g. with raw SQL),
Postgres cascades the delete to guardian rows too. The cascade in the relationship
covers deletes that go through SQLAlchemy; the `ondelete` covers everything else.

```python
    preferred_channel: Mapped[Channel] = mapped_column(str_enum(Channel), default=Channel.EMAIL)
```

`default=Channel.EMAIL` is the Python-side default — used when you create a `Guardian()`
without specifying `preferred_channel`. The DB column has no DEFAULT; SQLAlchemy
supplies the value before the INSERT.

### Enrollment

```python
    __table_args__ = (
        CheckConstraint("monthly_fee >= 0", name="fee_non_negative"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates_ordered"),
    )
```

`CheckConstraint` adds a rule the database enforces on every INSERT or UPDATE.
`end_date IS NULL OR end_date >= start_date` means: either no end date (still enrolled),
or the end date must be on or after the start date (can't end before starting).

```python
    def is_active_on(self, day: date) -> bool:
        return self.start_date <= day and (self.end_date is None or day <= self.end_date)
```

A regular Python method on the model class. The billing engine calls this to check
whether an enrollment should generate a charge for a given month.

---

## `src/tuition/db/session.py` — connecting to the database

```python
def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url)
```

An `Engine` is SQLAlchemy's connection pool manager. It doesn't open a connection
immediately — only when you first ask for one. `create_engine()` validates the URL
format but doesn't try to connect.

```python
@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
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
```

`@contextmanager` turns this function into a `with` block:

```python
with session_scope() as session:
    session.add(Family(external_key="F001", display_name="Smith"))
# ← commit() called here automatically
```

The try/except/finally guarantees:
- If everything in the `with` block succeeds → `commit()` (write to DB)
- If anything raises an exception → `rollback()` (undo all changes in this block)
- Always → `close()` (return connection to pool)

`expire_on_commit=False` → after a commit, object attributes stay readable.
Default behaviour would "expire" them, making the next attribute access trigger
another SQL query. Since we often return objects after saving, keeping them
readable is simpler.

---

## `alembic/env.py` — wiring Alembic to our code

The two key additions to the generated default:

```python
import tuition.db.models as _models  # noqa: F401 — side-effect import
from tuition.db.base import Base
target_metadata = Base.metadata
```

`# noqa: F401` suppresses the linter warning "imported but unused" — we import
`models` only so the table classes execute and register themselves on `Base.metadata`.
If we skip this import, Alembic sees an empty schema and generates a migration
that does nothing.

```python
from tuition.config import get_settings
context.config.set_main_option("sqlalchemy.url", get_settings().database_url)
```

Overrides the URL in `alembic.ini` with the real one from `.env`. This way
there's one source of truth for the database URL, and Alembic automatically
picks up whatever is in `.env`.

---

## `alembic/versions/4a9abceb93c7_create_roster_tables.py` — the migration

This file was **generated automatically** by:

```bash
uv run alembic revision --autogenerate -m "create_roster_tables"
```

Alembic compared `Base.metadata` (our Python models) with the current database
schema (empty) and generated the SQL to close the gap.

```python
revision: str = '4a9abceb93c7'
down_revision: Union[str, Sequence[str], None] = None
```

Each migration has a unique ID. `down_revision = None` means this is the first
migration (no parent). Future migrations will have `down_revision = '4a9abceb93c7'`.

```python
def upgrade() -> None:
    op.create_table('families', ...)
    op.create_table('guardians', ...)
    ...
```

`upgrade()` runs when you apply the migration (`alembic upgrade head`).
`downgrade()` runs when you revert it (`alembic downgrade -1`).

The migration is applied with:
```bash
make migrate   # → uv run alembic upgrade head
```

Alembic tracks which migrations have run in the `alembic_version` table in the DB.

---

## `tests/conftest.py` — shared test fixtures

```python
TEST_URL = "sqlite://"
```

`sqlite://` with no path = in-memory SQLite. Each test process gets a fresh one.
SQLite doesn't need Docker, no port conflicts, and tests run in milliseconds.

```python
@pytest.fixture()
def engine():
    e = create_engine(TEST_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)
    e.dispose()
```

`@pytest.fixture()` marks a function that sets up (and tears down) a resource.
Any test that declares `engine` as a parameter gets its own fresh copy.

`Base.metadata.create_all(e)` → creates all tables in the test DB from our models.
`yield e` → hands the engine to the test, then resumes when the test finishes.
`drop_all` + `dispose` → clean slate; the next test starts fresh.

```python
@pytest.fixture()
def session(engine) -> Session:
    with Session(engine) as s:
        yield s
        s.rollback()
```

The `session` fixture uses the `engine` fixture (pytest injects it automatically).
`s.rollback()` after `yield` means even if a test called `session.commit()`,
the data disappears after the test. Tests are always isolated.

---

## Running everything

```bash
# Install the new packages (sqlalchemy, alembic, psycopg)
uv sync

# Start Postgres
make up

# Create the tables
make migrate

# Run the tests (no Docker needed)
make test
# → 7 passed in 0.10s
```

---

## The table relationships as a diagram

```
families
  │
  ├── guardians  (many per family, FK: family_id → families.id)
  │
  └── students   (many per family, FK: family_id → families.id)
        │
        └── enrollments  (many per student, FK: student_id → students.id)
```

Delete a family → cascade → its guardians and students → cascade → their enrollments.

---

## What's next: Step 2b

The money tables: `charges` (what a family owes), `transactions` (PayPal/bank
payments we imported), and `allocations` (which payment paid which charge).
These are the rows the reconciliation agent will read and write.
