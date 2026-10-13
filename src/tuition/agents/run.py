"""
src/tuition/agents/run.py
--------------------------
CLI entrypoint for the tuition agent graph.

Usage
-----
  python -m tuition.agents.run 2026-01-01 \\
      data/seed/paypal_jan2026.csv \\
      data/seed/bank_jan2026.csv

What happens
------------
1. Graph runs ingestion → reconciliation → PAUSE (human review).
2. CLI prints pending charges and waits for operator input (y/n).
3. If approved, graph resumes → reminder node drafts messages.
4. Final summary printed: charges, matched, pending, reminders drafted, total LLM cost.

Thread ID
---------
Each run uses billing_period as the thread_id so the checkpointer
can resume correctly if the process is restarted between step 1 and step 3.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tuition.agents.graph import build_graph
from tuition.agents.state import AgentState


def _print_section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print('─' * 60)


def main(billing_period: str, paypal_csv: str, bank_csv: str) -> None:
    # ── Build graph with interrupt enabled ────────────────────────────────
    graph = build_graph(interrupt_before_reminder=True)
    thread = {"configurable": {"thread_id": billing_period}}

    initial_state: AgentState = {
        "billing_period": billing_period,
        "paypal_csv_path": str(Path(paypal_csv).resolve()),
        "bank_csv_path": str(Path(bank_csv).resolve()),
        "total_llm_cost_usd": 0.0,
        "errors": [],
    }

    # ── Phase 1: ingestion + reconciliation (runs until interrupt) ─────────
    _print_section(f"Phase 1 — Ingestion & Reconciliation  [{billing_period}]")
    state = graph.invoke(initial_state, thread)

    # Show ingestion summary
    ing = state.get("ingestion_result", {})
    pp = ing.get("paypal", {})
    bk = ing.get("bank", {})
    print(f"\n  PayPal  : {pp.get('inserted', 0)} inserted, {pp.get('skipped', 0)} skipped")
    print(f"  Bank    : {bk.get('inserted', 0)} inserted, {bk.get('skipped', 0)} skipped")

    # Show reconciliation summary
    rec = state.get("reconcile_result", {})
    print(f"\n  Matched : {rec.get('matched', 0)}")
    print(f"  Partial : {rec.get('partial', 0)}")
    print(f"  Unmatched: {rec.get('unmatched', 0)}")
    print(f"  Skipped : {rec.get('skipped', 0)}")

    # LLM assist results
    llm_assists = state.get("llm_assist_results", [])
    if llm_assists:
        print(f"\n  LLM assisted {len(llm_assists)} hard case(s):")
        for r in llm_assists:
            print(f"    {r['external_id']} → family {r['family_id']} "
                  f"({r['confidence']} confidence): {r['notes']}")

    # ── Human review ───────────────────────────────────────────────────────
    pending = state.get("pending_charges", [])
    _print_section(f"Human Review — {len(pending)} pending charge(s)")

    if not pending:
        print("\n  All charges reconciled — no reminders needed.")
        print(f"\n  Total LLM cost: ${state.get('total_llm_cost_usd', 0.0):.6f}")
        return

    for charge in pending:
        print(
            f"\n  • {charge['family_name']} / {charge['student_name']}"
            f"  [{charge['program']}]  ${charge['amount_due']}"
            f"  → {charge.get('guardian_email', 'no email')}"
        )

    errors = state.get("errors", [])
    if errors:
        print(f"\n  Errors encountered:")
        for e in errors:
            print(f"    ⚠  {e}")

    print(f"\n  Approve sending reminders to {len(pending)} family/families? [y/N] ", end="")
    answer = input().strip().lower()

    if answer != "y":
        print("\n  Reminders not sent. Run again and approve when ready.")
        print(f"\n  Total LLM cost so far: ${state.get('total_llm_cost_usd', 0.0):.6f}")
        return

    # ── Phase 2: resume graph → reminder node ─────────────────────────────
    _print_section("Phase 2 — Drafting Reminders")

    final_state = graph.invoke(
        {"human_approved": True},
        thread,
    )

    reminders = final_state.get("reminder_results", [])
    for r in reminders:
        print(f"\n  ── {r['family_name']} ({r.get('guardian_email', '')}) ──")
        print(f"  {r['message_preview']}")

    _print_section("Done")
    print(f"  Reminders drafted : {len(reminders)}")
    print(f"  Total LLM cost    : ${final_state.get('total_llm_cost_usd', 0.0):.6f}")
    print()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python -m tuition.agents.run <billing_period> <paypal_csv> <bank_csv>")
        print("Example: python -m tuition.agents.run 2026-01-01 data/seed/paypal_jan2026.csv data/seed/bank_jan2026.csv")
        sys.exit(1)

    main(
        billing_period=sys.argv[1],
        paypal_csv=sys.argv[2],
        bank_csv=sys.argv[3],
    )
