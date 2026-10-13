"""
src/tuition/agents/reconciliation.py
--------------------------------------
Reconciliation node: generates charges, runs reconciliation, then uses
the LLM only for hard cases that the rule-based reconciler couldn't match.

What this node does
-------------------
1. Calls run_billing_tool  → creates Charge rows for all active enrollments.
2. Calls reconcile_all_pending_tool → matches transactions to charges by name.
3. Inspects the results: any "unmatched" transactions go to the LLM.
4. LLM looks at the unmatched transaction (payer name, amount, memo) and the
   list of families with open charges — it decides which family it belongs to
   and with what confidence. This covers name-mismatch (husband pays, wife is
   primary) and split-payment cases.
5. Collects pending_charges (charges still unpaid after reconcile).
6. Logs every LLM call to the llm_calls table (feeds Step 9 dashboard).

Why the LLM only for hard cases?
---------------------------------
Running every transaction through Claude would be slow and expensive.
The rule-based reconciler handles ~90% of cases (exact name match).
The LLM is the fallback for the 10% that need fuzzy reasoning.
This pattern — rules first, LLM as escalation — is the right agentic design.

LLM cost tracking
-----------------
Every call to Claude records:
  model, input_tokens, output_tokens, cost_usd, agent="reconciliation",
  billing_period, external_id of the transaction being resolved.
These rows power the model-cost dashboard in Step 9.
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal

from tuition.agents.state import AgentState
from tuition.db.session import session_scope
from tuition.tools.mcp_server import reconcile_all_pending_tool, run_billing_tool

# ── Anthropic pricing (as of mid-2026, claude-3-5-haiku) ──────────────────────
# We use Haiku for reconciliation — fast, cheap, good enough for name matching.
_MODEL = "claude-3-5-haiku-20241022"
_COST_PER_1K_INPUT = Decimal("0.00025")   # $0.25 per million input tokens
_COST_PER_1K_OUTPUT = Decimal("0.00125")  # $1.25 per million output tokens


def _compute_cost(input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) / 1000 * _COST_PER_1K_INPUT
        + Decimal(output_tokens) / 1000 * _COST_PER_1K_OUTPUT
    )


def _log_llm_call(
    *,
    billing_period: str,
    external_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
    agent: str,
) -> None:
    """Write one AgentRun + LlmCall row to record this LLM usage."""
    from tuition.db.models import AgentRun, AgentStatus, LlmCall
    from datetime import datetime, timezone

    with session_scope() as session:
        run = AgentRun(
            agent_name=agent,
            label=f"{billing_period} / {external_id}",
            status=AgentStatus.COMPLETED,
            finished_at=datetime.now(timezone.utc),
        )
        session.add(run)
        session.flush()
        session.add(LlmCall(
            agent_run_id=run.id,
            model=model,
            purpose=f"match {external_id}",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=float(cost_usd),
        ))


def _llm_match(
    *,
    txn: dict,
    open_families: list[dict],
    billing_period: str,
) -> dict:
    """Ask Claude to match one unmatched transaction to a family.

    Parameters
    ----------
    txn : dict
        The unmatched transaction: { external_id, payer_name, amount, memo, source }
    open_families : list[dict]
        Families with open charges: [{ family_id, display_name, guardians, amount_due }]
    billing_period : str
        "2026-01-01"

    Returns
    -------
    dict
        { external_id, llm_decision, family_id, confidence, notes }
        llm_decision is "matched", "partial", or "unmatched"
        confidence is "high", "medium", or "low"
    """
    try:
        import anthropic
    except ImportError:
        return {
            "external_id": txn["external_id"],
            "llm_decision": "unmatched",
            "family_id": None,
            "confidence": "low",
            "notes": "anthropic package not installed — skipping LLM assist",
        }

    prompt = f"""You are helping reconcile school tuition payments.

A payment came in that couldn't be automatically matched to a family:

PAYMENT:
  External ID : {txn['external_id']}
  Payer name  : {txn['payer_name']}
  Amount      : ${txn['amount']}
  Memo/Note   : {txn.get('memo', '(none)')}
  Source      : {txn['source']}

FAMILIES WITH OPEN CHARGES FOR {billing_period}:
{json.dumps(open_families, indent=2)}

Task: Decide which family this payment most likely belongs to.

Reply with ONLY valid JSON (no markdown, no explanation):
{{
  "family_id": <int or null>,
  "confidence": "high" | "medium" | "low",
  "reasoning": "<one sentence>"
}}

If you cannot confidently match it, set family_id to null and confidence to "low".
"""

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    response = client.messages.create(
        model=_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost_usd = _compute_cost(input_tokens, output_tokens)

    # Log cost
    _log_llm_call(
        billing_period=billing_period,
        external_id=txn["external_id"],
        model=_MODEL,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        agent="reconciliation",
    )

    raw = response.content[0].text.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"family_id": None, "confidence": "low", "reasoning": raw}

    return {
        "external_id": txn["external_id"],
        "llm_decision": "matched" if parsed.get("family_id") else "unmatched",
        "family_id": parsed.get("family_id"),
        "confidence": parsed.get("confidence", "low"),
        "notes": parsed.get("reasoning", ""),
        "cost_usd": float(cost_usd),
    }


def _get_open_families(billing_period: str) -> list[dict]:
    """Fetch families with pending charges for the billing period."""
    from sqlalchemy import select

    from tuition.db.models import Charge, Family, Guardian

    period = date.fromisoformat(billing_period[:7] + "-01")
    results = []

    with session_scope() as session:
        charges = session.scalars(
            select(Charge).where(
                Charge.billing_period == period,
                Charge.status == "pending",
            )
        ).all()

        for charge in charges:
            family = session.get(Family, charge.family_id)
            if not family:
                continue
            guardians = session.scalars(
                select(Guardian).where(Guardian.family_id == family.id)
            ).all()
            results.append({
                "family_id": family.id,
                "display_name": family.display_name,
                "guardians": [g.full_name for g in guardians],
                "amount_due": str(charge.amount_due),
            })

    return results


def _get_pending_charges(billing_period: str) -> list[dict]:
    """Return all charges still in 'pending' status after reconciliation."""
    from sqlalchemy import select

    from tuition.db.models import Charge, Enrollment, Family, Guardian, Student

    period = date.fromisoformat(billing_period[:7] + "-01")
    rows = []

    with session_scope() as session:
        charges = session.scalars(
            select(Charge).where(
                Charge.billing_period == period,
                Charge.status == "pending",
            )
        ).all()

        for charge in charges:
            family = session.get(Family, charge.family_id)
            enrollment = session.get(Enrollment, charge.enrollment_id)
            student = session.get(Student, enrollment.student_id) if enrollment else None
            guardian = session.scalars(
                select(Guardian).where(
                    Guardian.family_id == charge.family_id,
                    Guardian.is_primary.is_(True),
                )
            ).first()

            rows.append({
                "charge_id": charge.id,
                "family_id": charge.family_id,
                "family_name": family.display_name if family else "Unknown",
                "enrollment_id": charge.enrollment_id,
                "program": enrollment.program if enrollment else "Unknown",
                "student_name": (
                    f"{student.first_name} {student.last_name}" if student else "Unknown"
                ),
                "amount_due": str(charge.amount_due),
                "guardian_email": guardian.email if guardian else None,
                "guardian_name": guardian.full_name if guardian else None,
            })

    return rows


def reconciliation_node(state: AgentState) -> AgentState:
    """LangGraph node: run billing + reconcile + LLM assist for hard cases.

    Reads
    -----
    state["billing_period"]  : str

    Writes
    ------
    state["billing_result"]      : dict
    state["reconcile_result"]    : dict
    state["llm_assist_results"]  : list
    state["pending_charges"]     : list
    state["total_llm_cost_usd"]  : float (accumulated)
    state["errors"]              : list (appended to)
    """
    billing_period = state["billing_period"]
    errors: list[str] = list(state.get("errors") or [])
    total_cost = float(state.get("total_llm_cost_usd") or 0.0)

    # ── Step 1: generate charges ───────────────────────────────────────────
    billing_result = run_billing_tool(billing_period)

    # ── Step 2: rule-based reconciliation ─────────────────────────────────
    reconcile_result = reconcile_all_pending_tool(billing_period)

    # ── Step 3: LLM assist for unmatched transactions ─────────────────────
    llm_assist_results: list[dict] = []
    unmatched = [
        r for r in reconcile_result.get("results", [])
        if r["outcome"] == "unmatched"
    ]

    if unmatched:
        open_families = _get_open_families(billing_period)
        for txn_result in unmatched:
            llm_result = _llm_match(
                txn={
                    "external_id": txn_result["external_id"],
                    "payer_name": txn_result.get("notes", ""),
                    "amount": txn_result.get("notes", ""),
                    "memo": "",
                    "source": "",
                },
                open_families=open_families,
                billing_period=billing_period,
            )
            total_cost += llm_result.get("cost_usd", 0.0)
            llm_assist_results.append(llm_result)

    # ── Step 4: collect pending charges ───────────────────────────────────
    pending_charges = _get_pending_charges(billing_period)

    return {
        **state,
        "billing_result": billing_result,
        "reconcile_result": reconcile_result,
        "llm_assist_results": llm_assist_results,
        "pending_charges": pending_charges,
        "total_llm_cost_usd": total_cost,
        "errors": errors,
    }
