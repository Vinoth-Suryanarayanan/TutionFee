"""
src/tuition/agents/reminder.py
--------------------------------
Reminder node: drafts a payment reminder message for each family with
a pending charge, using the LLM to personalise the wording.

What this node does
-------------------
1. Reads pending_charges from state (set by reconciliation_node).
2. For each pending charge, asks Claude to draft a short, polite reminder.
3. Records draft text + estimated LLM cost in reminder_results.
4. Logs every LLM call to the llm_calls table (Step 9 dashboard).

This node only runs after human_review_node has set human_approved=True.
The graph enforces this — if the operator doesn't approve, the graph ends
and no reminders are sent.

NOTE: This node drafts messages — it does NOT send them.
Sending (email/SMS/WhatsApp) is out of scope for the local-first MVP.
The drafted text is shown in the CLI output and stored in state for review.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

from tuition.agents.state import AgentState

_MODEL = "claude-3-5-haiku-20241022"
_COST_PER_1K_INPUT = Decimal("0.00025")
_COST_PER_1K_OUTPUT = Decimal("0.00125")


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
) -> None:
    from datetime import datetime, timezone

    from tuition.db.models import AgentRun, AgentStatus, LlmCall
    from tuition.db.session import session_scope

    with session_scope() as session:
        run = AgentRun(
            agent_name="reminder",
            label=f"{billing_period} / {external_id}",
            status=AgentStatus.COMPLETED,
            finished_at=datetime.now(timezone.utc),
        )
        session.add(run)
        session.flush()
        session.add(LlmCall(
            agent_run_id=run.id,
            model=model,
            purpose=f"reminder {external_id}",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=float(cost_usd),
        ))


def _draft_reminder(*, charge: dict, billing_period: str) -> dict:
    """Ask Claude to draft a polite tuition reminder for one family.

    Returns dict with: family_id, guardian_name, channel, message_preview, cost_usd
    """
    try:
        import anthropic
    except ImportError:
        return {
            "family_id": charge["family_id"],
            "guardian_name": charge.get("guardian_name"),
            "channel": "email",
            "message_preview": (
                f"Hi {charge.get('guardian_name', 'there')}, "
                f"this is a reminder that {charge['student_name']}'s "
                f"{charge['program']} tuition of ${charge['amount_due']} "
                f"for {billing_period[:7]} is still outstanding. "
                "Please let us know if you have any questions."
            ),
            "cost_usd": 0.0,
            "note": "anthropic not installed — used template fallback",
        }

    prompt = f"""Draft a short, warm, professional tuition payment reminder.

School: Vrukshaa Music & Yoga Academy
Student: {charge['student_name']}
Program: {charge['program']}
Amount due: ${charge['amount_due']}
Month: {billing_period[:7]}
Parent name: {charge.get('guardian_name', 'Parent')}

Requirements:
- Friendly and non-threatening tone (these are families we know personally)
- 3-4 sentences maximum
- End with an offer to help if they have questions
- Do NOT mention late fees or consequences

Reply with ONLY valid JSON:
{{
  "subject": "<email subject line>",
  "body": "<message body, plain text, no markdown>"
}}
"""

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    response = client.messages.create(
        model=_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost_usd = _compute_cost(input_tokens, output_tokens)

    _log_llm_call(
        billing_period=billing_period,
        external_id=f"charge-{charge['charge_id']}",
        model=_MODEL,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )

    raw = response.content[0].text.strip()
    try:
        parsed = json.loads(raw)
        preview = f"Subject: {parsed.get('subject', '')}\n\n{parsed.get('body', '')}"
    except json.JSONDecodeError:
        preview = raw

    return {
        "family_id": charge["family_id"],
        "family_name": charge["family_name"],
        "guardian_name": charge.get("guardian_name"),
        "guardian_email": charge.get("guardian_email"),
        "channel": "email",
        "message_preview": preview,
        "cost_usd": float(cost_usd),
    }


def reminder_node(state: AgentState) -> AgentState:
    """LangGraph node: draft reminders for all pending charges.

    Reads
    -----
    state["pending_charges"]     : list
    state["billing_period"]      : str
    state["human_approved"]      : bool  (must be True — graph enforces this)

    Writes
    ------
    state["reminder_results"]    : list
    state["total_llm_cost_usd"]  : float (accumulated)
    """
    billing_period = state["billing_period"]
    pending_charges = state.get("pending_charges") or []
    total_cost = float(state.get("total_llm_cost_usd") or 0.0)

    reminder_results = []
    for charge in pending_charges:
        result = _draft_reminder(charge=charge, billing_period=billing_period)
        total_cost += result.get("cost_usd", 0.0)
        reminder_results.append(result)

    return {
        **state,
        "reminder_results": reminder_results,
        "total_llm_cost_usd": total_cost,
    }
