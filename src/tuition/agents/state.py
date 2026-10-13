"""
src/tuition/agents/state.py
----------------------------
Shared state TypedDict that flows between every node in the LangGraph graph.

Think of it as the "job ticket" that gets passed from agent to agent.
Each node reads what it needs and writes its results back to the same dict.

Fields
------
billing_period : str
    ISO date for the billing month, e.g. "2026-01-01".
    Set by the caller (run.py) before the graph starts.

paypal_csv_path : str
    Absolute path to the PayPal CSV export on disk.
    Set by the caller; read by the ingestion node.

bank_csv_path : str
    Absolute path to the bank statement CSV on disk.
    Set by the caller; read by the ingestion node.

ingestion_result : dict
    Written by the ingestion node after importing both CSVs.
    Keys: paypal (ImportResult dict), bank (ImportResult dict).

billing_result : dict
    Written by the reconciliation node after run_billing_tool.
    Keys: period, charges_created, charges list.

reconcile_result : dict
    Written by the reconciliation node after reconcile_all_pending_tool.
    Keys: period, total, matched, partial, unmatched, no_charges, skipped, results.

llm_assist_results : list[dict]
    Written by the reconciliation node for any unmatched transactions
    that were resolved by the LLM (name-mismatch, split-payment cases).
    Each dict: { transaction_id, external_id, llm_decision, confidence, notes }

pending_charges : list[dict]
    Written by the reconciliation node: charges still in "pending" status
    after reconciliation — these are the families who haven't paid yet.
    Each dict: { charge_id, family_id, enrollment_id, amount_due, student_name }

human_approved : bool
    Set to True by the human_review node (interrupt) once the operator
    reviews the pending list and approves sending reminders.
    Defaults to False — graph will pause before the reminder node.

reminder_results : list[dict]
    Written by the reminder node after drafting messages for each pending family.
    Each dict: { family_id, channel, message_preview, llm_cost_usd }

total_llm_cost_usd : float
    Running total of LLM spend across all nodes. Updated by any node
    that calls the LLM. Used by the model-cost dashboard (Step 9).

errors : list[str]
    Any non-fatal errors collected along the way.
    Fatal errors raise exceptions; soft errors (e.g. CSV parse failure) land here.
"""

from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict, total=False):
    # ── Inputs (set by caller before graph starts) ─────────────────────────
    billing_period: str        # "2026-01-01"
    paypal_csv_path: str       # "/abs/path/paypal_jan2026.csv"
    bank_csv_path: str         # "/abs/path/bank_jan2026.csv"

    # ── Ingestion node outputs ──────────────────────────────────────────────
    ingestion_result: dict     # { paypal: {...}, bank: {...} }

    # ── Reconciliation node outputs ─────────────────────────────────────────
    billing_result: dict       # run_billing_tool response
    reconcile_result: dict     # reconcile_all_pending_tool response
    llm_assist_results: list   # LLM decisions for hard-match cases
    pending_charges: list      # charges still unpaid after reconcile

    # ── Human-review node (interrupt) ──────────────────────────────────────
    human_approved: bool       # True once operator approves sending reminders

    # ── Reminder node outputs ───────────────────────────────────────────────
    reminder_results: list     # one entry per family reminded

    # ── Cross-cutting ───────────────────────────────────────────────────────
    total_llm_cost_usd: float  # accumulates across all LLM calls
    errors: list               # non-fatal errors
