# Vrukshaa Tuition Fee Tracker — Architecture

**School:** Vrukshaa Music Academy  
**Programs:** Carnatic ($40/mo), Yoga ($40/mo), Both ($80/mo per student)  
**Purpose:** Track monthly tuition, import PayPal/bank statements, match payments to charges, send reminders to parents.  
**Design goal:** Agentic architecture showcase — local-first, no cloud required, every decision testable.

---

## 1. Project Layout

```
tuition-fee-agents/
├── src/tuition/               # Application code (Python package)
│   ├── config.py              # DATABASE_URL, env vars
│   ├── billing.py             # Step 4 — generate monthly Charge rows
│   ├── importer.py            # Step 5 — load PayPal/bank CSV → transactions table
│   ├── reconciler.py          # Step 6 — match transactions to charges → allocations
│   └── db/
│       ├── base.py            # SQLAlchemy Base, str_enum helper, utcnow
│       ├── models.py          # All 10 ORM table classes (Steps 2a/2b/2c)
│       └── session.py         # get_session() factory
│
├── tests/                     # Pytest test suite (93 tests)
│   ├── conftest.py            # Shared fixtures: engine, session (in-memory SQLite)
│   ├── test_roster_models.py  # Step 2a — Family, Guardian, Student, Enrollment
│   ├── test_money_models.py   # Step 2b — Charge, Transaction, Allocation
│   ├── test_agent_models.py   # Step 2c — AgentRun, LlmCall, Reminder
│   ├── test_billing.py        # Step 4 — run_billing()
│   ├── test_importer.py       # Step 5 — import_paypal_csv(), import_bank_csv()
│   └── test_reconciler.py     # Step 6 — reconcile_transaction(), reconcile_all_pending()
│
├── scripts/
│   └── seed.py                # Load mock data (families, enrollments, charges, transactions)
│
├── data/seed/
│   ├── families.json          # 10 mock families (FAM001–FAM010) with guardians + students
│   ├── enrollments.json       # 13 enrollments (STU004 Ethan has both Carnatic + Yoga)
│   ├── paypal_jan2026.csv     # 11 PayPal rows (includes duplicate, unknown payer, partial)
│   ├── bank_jan2026.csv       # 3 bank rows (partial completion, cross-channel duplicate)
│   └── ground_truth.json      # Expected reconciliation output for testing agents
│
├── alembic/                   # Database migrations (hand-written, no autogenerate)
├── dev.db                     # SQLite development database
├── pyproject.toml             # Project metadata, dependencies, pytest config
├── Makefile                   # make setup, make test, make doctor
└── ARCHITECTURE.md            # This file
```

---

## 2. Database Schema (10 tables)

All money columns use `Numeric(10, 2)` (stored as Decimal, never float).  
All enums are stored as `VARCHAR + CHECK` constraint via the `str_enum()` helper (works on both SQLite and Postgres).

### Step 2a — Roster tables

```
families
├── id (PK)
├── external_key  UNIQUE    "FAM001"
├── display_name             "Smith Family"
├── notes
└── created_at

guardians
├── id (PK)
├── family_id → families.id  (CASCADE DELETE)
├── full_name                "Robert Smith"
├── email
├── phone
├── relation_to_student      "father"
├── is_primary               true/false
└── preferred_channel        "email" | "sms"

students
├── id (PK)
├── external_key  UNIQUE    "STU001"
├── family_id → families.id  (CASCADE DELETE)
├── first_name
└── last_name

enrollments
├── id (PK)
├── student_id → students.id  (CASCADE DELETE)
├── program                  "Carnatic" | "Yoga"
├── monthly_fee  Numeric     40.00
├── start_date               2025-09-01
└── end_date                 NULL = still active
    method: is_active_on(day) → bool
    CHECK: monthly_fee >= 0
    CHECK: end_date IS NULL OR end_date >= start_date
```

**Key relationships:**  
`Family 1──* Guardian` (who to contact)  
`Family 1──* Student 1──* Enrollment` (who is in which class)

---

### Step 2b — Money tables

```
charges
├── id (PK)
├── enrollment_id → enrollments.id  (CASCADE DELETE)
├── family_id → families.id          (CASCADE DELETE) ← DENORMALIZED for fast lookup
├── billing_period  Date             2026-01-01 = "January 2026"
├── amount_due  Numeric              40.00
├── status                           "pending" | "paid" | "partial" | "waived"
├── notes
└── created_at
    property: amount_paid   → sum of allocation.amount for this charge
    property: amount_remaining → amount_due - amount_paid
    UNIQUE: (enrollment_id, billing_period)  ← idempotency guard
    CHECK: amount_due >= 0

transactions
├── id (PK)
├── family_id → families.id  (SET NULL on delete) ← NULL until matched
├── source                   "paypal" | "bank" | "manual"
├── external_id              "PP-JAN001" (PayPal txn ID or bank reference)
├── paid_on  Date
├── amount  Numeric          40.00
├── payer_name               "Robert Smith" (raw from CSV)
├── memo                     "Alice Carnatic Jan 2026"
└── imported_at
    property: amount_allocated → sum of allocation.amount for this transaction
    property: amount_unallocated → amount - amount_allocated
    UNIQUE: (source, external_id)  ← idempotency guard for re-import

allocations
├── id (PK)
├── transaction_id → transactions.id  (CASCADE DELETE)
├── charge_id → charges.id            (CASCADE DELETE)
├── amount  Numeric                   40.00  (portion of transaction applied to charge)
├── confirmed_by_human               false = agent-matched, true = human reviewed
├── notes
└── created_at
    CHECK: amount > 0
```

**Key relationships:**  
`Transaction *──* Charge` via `Allocation` (one transaction can cover multiple charges; one charge can receive payments from multiple transactions — e.g. split partial payments across PayPal + bank)

Why `family_id` is denormalized on `Charge`:  
Without it, "show me all charges for Smith Family" requires: `charges → enrollments → students → families` (3 joins). With it: `charges WHERE family_id = ?` (1 filter). This matters when the matching agent iterates over many families.

---

### Step 2c — Agent observability tables

```
agent_runs
├── id (PK)
├── agent_name               "ingestion" | "reconciliation" | "reminder" | "orchestrator"
├── label                    "January 2026 billing"
├── status                   "running" | "succeeded" | "failed" | "cancelled"
├── started_at
├── finished_at
└── error_message
    property: duration_seconds → (finished_at - started_at).total_seconds()
    property: total_cost_usd  → sum of llm_calls.cost_usd

llm_calls
├── id (PK)
├── agent_run_id → agent_runs.id  (CASCADE DELETE)
├── model                    "claude-sonnet-4-6"
├── purpose                  "match_payment" | "draft_reminder"
├── input_tokens             1234
├── output_tokens            89
├── cost_usd                 0.00182   ← computed at call time
├── latency_ms               743
└── called_at
    property: total_tokens → input_tokens + output_tokens

reminders
├── id (PK)
├── agent_run_id → agent_runs.id  (CASCADE DELETE)
├── family_id → families.id       (CASCADE DELETE)
├── guardian_id → guardians.id    (SET NULL)
├── channel                  "email" | "sms"
├── body                     full message text (stored for audit)
├── charge_ids_json          "[12, 13]"  snapshot of what was owed at send time
├── status                   "pending" | "sent" | "delivered" | "failed"
├── sent_at
└── created_at
    method: get_charge_ids() → list[int]
    method: set_charge_ids(ids: list[int])
```

---

## 3. Module Map and Data Flow

```
Real CSV files
     │
     ▼
┌──────────────────────────────────┐
│  importer.py                     │
│  import_paypal_csv(session, path)│  ← reads PayPal 32-column CSV
│  import_bank_csv(session, path)  │  ← reads bank Date/Desc/Amount/Ref CSV
│                                  │
│  Returns: ImportResult           │
│    .inserted   new rows added    │
│    .skipped    already in DB     │
│    .duplicate_in_csv  CSV dups   │
│    .errors     parse failures    │
└──────────┬───────────────────────┘
           │ writes Transaction rows (family_id=NULL)
           ▼
    transactions table

           │
           ▼
┌──────────────────────────────────┐
│  billing.py                      │
│  run_billing(session, period)    │  ← called once per month
│                                  │
│  Reads: all active Enrollments   │
│  Writes: one Charge per active   │
│          enrollment (idempotent) │
│  Returns: list[Charge] (new ones)│
└──────────┬───────────────────────┘
           │ writes Charge rows (status=PENDING)
           ▼
    charges table

           │
           ▼
┌──────────────────────────────────────────┐
│  reconciler.py                           │
│                                          │
│  find_family_by_payer_name(session, name)│
│    1. Exact guardian.full_name match     │
│    2. Case-insensitive guardian match    │
│    3. Case-insensitive family.display_name│
│    Returns: Family | None                │
│                                          │
│  reconcile_transaction(session, txn, period)│
│    → find family (name match or family_id)  │
│    → find open charges for family+period    │
│    → allocate_to_charges()                  │
│    → update charge.status (PAID/PARTIAL)    │
│    Returns: ReconcileResult                 │
│      .outcome  matched|unmatched|no_charges │
│                skipped|partial              │
│                                          │
│  reconcile_all_pending(session, period)  │
│    → processes every unallocated txn     │
│    Returns: list[ReconcileResult]        │
└──────────┬───────────────────────────────┘
           │ writes Allocation rows
           │ updates Charge.status
           ▼
    allocations table + charges updated

           │  (future steps)
           ▼
┌──────────────────────────────────┐
│  LangGraph agents (Step 8)       │
│  • ingestion agent               │
│  • reconciliation agent (LLM for │
│    hard cases: name mismatch,    │
│    cross-channel dup, split pay) │
│  • ledger agent                  │
│  • reminder agent                │
│  • orchestrator (human-in-loop)  │
└──────────┬───────────────────────┘
           │ writes AgentRun, LlmCall, Reminder rows
           ▼
    agent_runs / llm_calls / reminders tables

           │
           ▼
┌──────────────────────────────────┐
│  Model-cost dashboard (Step 9)   │
│  Reads: llm_calls table          │
│  Shows: tokens used, $ per agent │
│         run, model comparison    │
└──────────────────────────────────┘
```

---

## 4. Idempotency Guarantees

Every write operation is safe to repeat.

| Operation | Guard | Mechanism |
|---|---|---|
| Import PayPal CSV | CSV-level + DB-level | `seen_ids` set in-memory; `UNIQUE(source, external_id)` in DB |
| Import bank CSV | DB-level | `UNIQUE(source, external_id)` in DB |
| Run billing | DB-level | `UNIQUE(enrollment_id, billing_period)` in DB; pre-check before insert |
| Reconcile transaction | Allocation check | Skips if transaction already has allocations |
| Seed families/students | DB-level | `get_or_none()` check on `external_key` before insert |

---

## 5. Test Suite

**Total: 93 passing, 1 skipped** (the skip is a SQLite-incompatible Postgres CHECK test).

| File | Module tested | # Tests | What is covered |
|---|---|---|---|
| `test_roster_models.py` | `db/models.py` (Step 2a) | 7 | Family/Guardian/Student/Enrollment creation, `is_active_on()`, cascade delete, CHECK constraints |
| `test_money_models.py` | `db/models.py` (Step 2b) | 10 + 1 skip | Charge status transitions, `amount_paid`/`amount_remaining` properties, Transaction `amount_unallocated`, Allocation math, UNIQUE constraint on (source, external_id) |
| `test_agent_models.py` | `db/models.py` (Step 2c) | 14 | AgentRun status, `duration_seconds`, `total_cost_usd`, LlmCall `total_tokens`, Reminder `get/set_charge_ids` |
| `test_billing.py` | `billing.py` | 17 | Active enrollment → charge created, idempotency (same period twice = 0 new), withdrawn/future enrollment skipped, edge dates (start==period, end==period), multi-program student ($80 total), multi-family |
| `test_importer.py` | `importer.py` | 23 | PayPal happy path, idempotency, CSV duplicates, email filter, zero/bad amounts, bad dates; Bank happy path, debit skip, idempotency, bad reference/date; `ImportResult` count correctness |
| `test_reconciler.py` | `reconciler.py` | 22 | Name matching (exact, case-insensitive, display name, ambiguous, unknown); charge allocation (exact, partial, multi-charge, overpayment); `reconcile_transaction` (matched, unmatched, no_charges, skipped, partial outcome); `reconcile_all_pending` (all unallocated processed, already-allocated skipped, source filter) |

---

## 6. Mock Data Edge Cases (January 2026)

These edge cases exist specifically to stress-test the matching agent (Step 8):

| Case | Transaction | Family | Detail |
|---|---|---|---|
| Name mismatch | PP-JAN002 (Raj Patel, $80) | FAM002 (Priya Patel) | Husband pays, wife is primary guardian. Memo "Arjun Carnatic + Meera Yoga" helps. |
| Partial + split | PP-JAN004 (Ana Garcia, $30) + BNK-JAN002 ($10) | FAM005 | $30 PayPal + $10 bank = $40 Yoga charge. Two transactions, one charge. |
| CSV duplicate | PP-JAN008 (Chidi Okafor, $40) appears TWICE in CSV | FAM009 | Importer deduplicates at CSV-parse level. |
| Unknown sender | PP-JAN009 ($40) | None | No family matched. Agent flags as unmatched. |
| Cross-channel dup | PP-JAN010 (Karen Johnson, $80) + BNK-JAN003 ($80) | FAM003 | Same payment via PayPal AND bank. Agent must detect and reject the bank row. |
| Two programs | STU004 Ethan (Carnatic + Yoga) | FAM003 | Two enrollments → two charges → $80/mo. One payment covers both. |

---

## 7. Key Design Decisions

**SQLAlchemy 2.0 declarative style with `Mapped[T]`**  
Every column is typed (e.g. `Mapped[Decimal]`, `Mapped[date | None]`). This makes IDE autocomplete reliable and catches wrong-type assignments at development time.

**`str_enum()` helper instead of native ENUM**  
SQLAlchemy's native ENUM type creates Postgres ENUM objects that are hard to migrate. `str_enum()` stores values as `VARCHAR + CHECK CONSTRAINT`, which works identically on SQLite (tests) and Postgres (production) and is easy to extend.

**`family_id` denormalized on `Charge` and `Reminder`**  
Avoids 3-hop joins in hot query paths. The tradeoff is an extra FK to maintain — worth it given the query patterns.

**`charge_ids_json` on `Reminder` instead of a join table**  
A reminder is a snapshot at a point in time. Storing charge IDs as JSON means the reminder record remains meaningful even if charges are later deleted or modified. A join table would silently lose that history on cascade delete.

**Decimal for money, Float for analytics**  
`Charge.amount_due`, `Transaction.amount`, `Allocation.amount` → `Numeric(10, 2)` / `Decimal`.  
`LlmCall.cost_usd` → `Float` — fractional-cent precision doesn't matter for dashboard aggregations.

**In-memory SQLite for tests, file SQLite for dev**  
Each test gets a fresh schema via `Base.metadata.create_all()` on `"sqlite:///:memory:"`. No Docker, no cleanup scripts. The seed script writes to `dev.db` (SQLite file) for manual inspection.

**Idempotent seed script**  
`scripts/seed.py` can be run multiple times without duplicating data. Useful during development when you reset the DB and re-seed.

---

## 8. What's Coming (Steps 7–10)

| Step | What | Why |
|---|---|---|
| 7 | MCP tool server | Expose `run_billing`, `import_paypal_csv`, `reconcile_all_pending` as MCP tools so agents can call them as structured function calls |
| 8 | LangGraph agents | Orchestrate the monthly workflow: ingest → reconcile → human review pause → send reminders. LLM used only for ambiguous match cases. |
| 9 | Model-cost dashboard | Read `llm_calls` table, render token usage and $ cost per agent run. Fulfills the project requirement to track AI cost, not just tuition. |
| 10 | FastAPI + Next.js frontend, GitHub Actions CI | Web UI for the teacher, shareable demo, CI badge. |

---

## 9. Step 7 — MCP Tool Server

**File:** `src/tuition/mcp_server.py`  
**Tests:** `tests/test_mcp_server.py` (15 tests)

### What it is

A [FastMCP](https://github.com/jlowin/fastmcp) server that wraps the three core Python functions as MCP tools — the same protocol Claude Desktop, Cursor, and Claude Code use to expose capabilities to agents.

### Why it exists

The LangGraph agents (Step 8) are separate processes. They can't `import` Python modules directly. The MCP server is the boundary: agents call a tool by name with JSON arguments, get a JSON result back. This is what makes the architecture genuinely agentic rather than just a Python script.

### Tools exposed

| Tool | Wraps | Returns |
|---|---|---|
| `run_billing_tool(billing_period)` | `billing.run_billing()` | `{period, charges_created, charges[]}` |
| `import_paypal_csv_tool(csv_path, recipient_email?)` | `importer.import_paypal_csv()` | `{inserted, skipped, duplicate_in_csv, errors[], total_rows_seen}` |
| `import_bank_csv_tool(csv_path)` | `importer.import_bank_csv()` | same shape as above |
| `reconcile_all_pending_tool(billing_period, source?)` | `reconciler.reconcile_all_pending()` | `{period, total, matched, partial, unmatched, no_charges, skipped, results[]}` |

### Key design points

- **Money as strings**: `amount_due` is returned as `"40.00"` not `40.0` — JSON floats can't represent Decimal exactly.
- **Dates as ISO strings**: `"2026-01-01"` — agents pass these back in as `billing_period`.
- **Day normalisation**: `billing_period="2026-01-15"` → treated as `2026-01-01`. Agents don't need to know the convention.
- **Missing file returns error, not exception**: the tool returns `{"errors": ["File not found: ..."]}` so the agent can log and continue rather than crashing.
- **One session per tool call**: each tool call opens, uses, commits, and closes its own DB session. Atomic and clean.

### Running it

```bash
# From project root (Mac terminal, venv active):
make mcp
# or
uv run python -m tuition.mcp_server
```

### Wiring it to Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "vrukshaa": {
      "command": "uv",
      "args": ["run", "python", "-m", "tuition.mcp_server"],
      "cwd": "/Users/ramyasubramanian/Projects/TutionFee"
    }
  }
}
```

Then restart Claude Desktop — it will show "vrukshaa" in the tools panel.

### Test coverage (15 tests)

| Class | Tests | What is covered |
|---|---|---|
| `TestParseBillingPeriod` | 3 | Valid date, day normalised to 1, bad string → ValueError |
| `TestRunBillingTool` | 3 | Happy path, idempotency, bad date |
| `TestImportPaypalCsvTool` | 4 | Happy path, missing file, idempotency, email filter |
| `TestImportBankCsvTool` | 2 | Happy path, missing file |
| `TestReconcileAllPendingTool` | 4 | Happy path matched, idempotency, source filter, bad source |

### What's Still Coming

| Step | What | Why |
|---|---|---|
| 8 | LangGraph agents | Orchestrate ingest → reconcile → human-review → remind as a graph with checkpoints |
| 9 | Model-cost dashboard | Read `llm_calls` table, render $ per agent run and tokens by model |
| 10 | FastAPI + Next.js, CI, README | Web UI, shareable demo, GitHub Actions CI badge |
