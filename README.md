# Tuition Fee Agents

A **local-first, multi-agent system** that tracks tuition payments for a small music/yoga school — reconciling PayPal and bank deposits against enrollments, escalating ambiguous payments to an LLM, and drafting parent reminders with a human-in-the-loop approval step before anything is sent.

Built as a portfolio project to demonstrate production-grade agentic AI design: layered architecture, LangGraph orchestration, MCP tool layer, cost tracking, and a full test suite — all running on your laptop with no cloud dependency.

> **Work in progress.** See [`docs/build-log/`](docs/build-log/) for a step-by-step walkthrough of how it is built.

---

## What it does

Each month, run the agent with a billing period and two CSV paths (PayPal activity export + bank statement). It:

1. **Ingests** both CSVs into a local Postgres database
2. **Generates charges** for every active enrollment (Carnatic $40/mo · Yoga $40/mo · Both $80/mo)
3. **Reconciles** transactions to charges by name-matching — covers ~90% of cases automatically
4. **Escalates** unmatched transactions (husband pays, wife is primary; split payments) to Claude Haiku for fuzzy matching — with cost logged per call
5. **Pauses for human review** before sending anything — you see every pending charge and approve
6. **Drafts reminders** for unpaid families — via Claude when an API key is present, or a clean template fallback

```
CSV files
    │
    ▼
┌─────────────┐    ┌──────────────────┐    ┌──────────────┐    ┌──────────────┐
│  ingestion  │───▶│ reconciliation   │───▶│ human_review │───▶│   reminder   │
│             │    │ rules → LLM      │    │  (interrupt) │    │ Claude/tmpl  │
└─────────────┘    └──────────────────┘    └──────────────┘    └──────────────┘
        │                   │                                          │
        └───────────────────┴──── LangGraph StateGraph ───────────────┘
                                  (MemorySaver checkpoint)
```

---

## Architecture

```
src/tuition/
├── db/          # SQLAlchemy models + session_scope
│   ├── models.py        # Family, Student, Guardian, Enrollment,
│   │                    # Charge, Transaction, AgentRun, LlmCall
│   └── session.py       # session_scope context manager
│
├── core/        # Pure business logic — no DB, no LLM
│   ├── importer.py      # PayPal CSV + bank CSV → Transaction rows
│   ├── billing.py       # Active enrollments → Charge rows
│   └── reconciler.py    # Rule-based name-match → paid/unmatched
│
├── tools/       # MCP tool layer — wraps core, owns DB session
│   └── mcp_server.py    # 7 @tool functions (import, bill, reconcile,
│                        # pending charges, reminders, …)
│
└── agents/      # LangGraph nodes + graph wiring
    ├── state.py         # AgentState TypedDict
    ├── ingestion.py     # node: ingest PayPal + bank CSVs
    ├── reconciliation.py# node: bill → reconcile → LLM assist
    ├── reminder.py      # node: draft reminders per pending charge
    ├── graph.py         # build_graph() — StateGraph wiring
    └── run.py           # CLI entry point
```

**Layer rule:** each layer imports only from the layer below it. `agents/` never touches `db/` directly — it calls through `tools/`, which own the session lifecycle. This makes every layer independently testable.

---

## Prerequisites

| Tool | Why | Install (macOS) |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Python + package manager | `brew install uv` |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Runs Postgres locally | `brew install --cask docker` then open it |
| make | Project commands | `xcode-select --install` |

Optional — only needed for LLM reconciliation assist and Claude-drafted reminders:

| Variable | What it unlocks |
|---|---|
| `ANTHROPIC_API_KEY` | LLM matching for ambiguous payments; Claude-written reminder text |

---

## Quick start

```bash
git clone https://github.com/Vinoth-Suryanarayanan/TutionFee.git
cd TutionFee

make setup      # install deps, copy .env.example → .env, check prerequisites
make doctor     # re-check anytime
make help       # list all commands
```

Start Postgres, apply migrations, seed mock data:

```bash
docker compose up -d
make migrate
make seed
```

Run the agent (mock data, no real API key needed):

```bash
uv run python -m tuition.agents.run \
  --billing-period 2026-01-01 \
  --paypal-csv data/seed/paypal_jan2026.csv \
  --bank-csv   data/seed/bank_jan2026.csv
```

Run the test suite:

```bash
make test       # 116 tests, ~2s, no Postgres needed
```

---

## Key design decisions

**Rules first, LLM as escalation.** Running every transaction through Claude would be slow and expensive. The rule-based reconciler handles ~90% of cases (exact name match). The LLM sees only the hard 10% — name mismatches, spouse payments, split amounts. This keeps cost low and keeps the fast path fast.

**MCP tool layer as the seam.** The seven `@tool` functions in `mcp_server.py` are the only place that touch `session_scope`. Every agent node calls through them, never directly to the DB. This makes the entire agent layer testable against an in-memory SQLite DB with a single `patch("tuition.tools.mcp_server.get_session", ...)`.

**Human-in-the-loop by default.** The graph compiles with `interrupt_before=["reminder"]`. The agent stops after reconciliation so you can review pending charges before any message is drafted or sent. Pass `interrupt_before_reminder=False` only in tests or batch runs.

**Cost tracking built in.** Every LLM call writes a row to `llm_calls` (model, input_tokens, output_tokens, cost_usd, agent, billing_period). This feeds a Step 9 model-cost dashboard — cost visibility is not an afterthought.

**No sensitive data committed.** `.env`, `*.db`, `*.sqlite`, and `data/private/` are all gitignored. Real parent names, emails, and payment records never leave your machine. `data/seed/` contains fictional families only.

---

## Pricing model (configurable)

| Program | Monthly fee |
|---|---|
| Carnatic music | $40 |
| Yoga | $40 |
| Both | $80 |

Fees are stored per-enrollment in the DB, not hardcoded. Change them in the seed data or directly in the `Enrollment` table.

---

## Build log

The [`docs/build-log/`](docs/build-log/) folder documents every step:

| Step | What was built |
|---|---|
| [01](docs/build-log/01-project-setup.md) | Project scaffold, `pyproject.toml`, `Makefile`, `make doctor` |
| [01b](docs/build-log/01b-make-doctor.md) | `make doctor` health checks |
| [01c](docs/build-log/01c-how-the-code-runs.md) | How all the pieces connect |
| [02a](docs/build-log/02a-database-and-models.md) | SQLAlchemy models + Alembic migrations |
| Steps 3–8 | Core layer, MCP tools, LangGraph agent, full test suite |

---

## License

MIT — see [LICENSE](LICENSE).
