"""Tests for the agent observability tables: AgentRun, LlmCall, Reminder.

These tests prove:
- agent runs track status and compute duration and total cost
- llm_calls accumulate under a run and their costs sum correctly
- reminders store the message body and charge list, link to family/guardian
- cascade: deleting a run also deletes its llm_calls and reminders
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tuition.db.models import (
    AgentRun,
    AgentStatus,
    Channel,
    Enrollment,
    Family,
    Guardian,
    LlmCall,
    Reminder,
    ReminderStatus,
    Student,
    Charge,
    Transaction,
    TransactionSource,
)


# -- Helpers ------------------------------------------------------------------


def make_family(session, *, key="F001", name="Smith Family") -> Family:
    f = Family(external_key=key, display_name=name)
    session.add(f)
    session.flush()
    return f


def make_guardian(session, family: Family) -> Guardian:
    g = Guardian(
        family_id=family.id,
        full_name="Bob Smith",
        email="bob@example.com",
        is_primary=True,
    )
    session.add(g)
    session.flush()
    return g


def make_run(session, *, name="reconciliation", label=None) -> AgentRun:
    r = AgentRun(agent_name=name, label=label)
    session.add(r)
    session.flush()
    return r


def make_llm_call(
    session,
    run: AgentRun,
    *,
    model="claude-sonnet-4-6",
    input_tokens=500,
    output_tokens=100,
    cost_usd=0.002,
) -> LlmCall:
    call = LlmCall(
        agent_run_id=run.id,
        model=model,
        purpose="match_payment",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_ms=340,
    )
    session.add(call)
    session.flush()
    return call


# -- AgentRun tests -----------------------------------------------------------


def test_agent_run_defaults(session):
    """A new run starts as RUNNING with no finish time."""
    run = make_run(session, name="ingestion", label="October 2025")

    assert run.id is not None
    assert run.status == AgentStatus.RUNNING
    assert run.finished_at is None
    assert run.duration_seconds is None   # can't compute without finish time
    assert run.total_cost_usd == 0.0      # no calls yet


def test_agent_run_duration(session):
    """duration_seconds is computed from started_at and finished_at."""
    now = datetime.now(UTC)
    run = AgentRun(
        agent_name="reconciliation",
        started_at=now,
        finished_at=now + timedelta(seconds=42),
        status=AgentStatus.SUCCEEDED,
    )
    session.add(run)
    session.flush()

    assert run.duration_seconds == pytest.approx(42.0)


def test_agent_run_total_cost(session):
    """total_cost_usd sums the cost of all llm_calls under this run."""
    run = make_run(session)
    make_llm_call(session, run, cost_usd=0.002)
    make_llm_call(session, run, cost_usd=0.005)
    session.refresh(run)

    assert run.total_cost_usd == pytest.approx(0.007)


def test_agent_run_total_cost_excludes_null(session):
    """LLM calls with cost_usd=None are skipped in the total (not counted as 0)."""
    run = make_run(session)
    call = LlmCall(
        agent_run_id=run.id,
        model="claude-haiku-5-5",
        input_tokens=100,
        output_tokens=20,
        cost_usd=None,   # cost unknown -- e.g. self-hosted model
    )
    session.add(call)
    make_llm_call(session, run, cost_usd=0.003)
    session.flush()
    session.refresh(run)

    # Only the call with a known cost contributes
    assert run.total_cost_usd == pytest.approx(0.003)


# -- LlmCall tests ------------------------------------------------------------


def test_llm_call_total_tokens(session):
    """total_tokens = input_tokens + output_tokens."""
    run = make_run(session)
    call = make_llm_call(session, run, input_tokens=800, output_tokens=200)

    assert call.total_tokens == 1000


def test_llm_call_stores_model_string(session):
    """The model field stores any string -- no enum constraint."""
    run = make_run(session)
    call = make_llm_call(session, run, model="claude-opus-5-5")
    assert call.model == "claude-opus-5-5"


def test_multiple_calls_per_run(session):
    """A single run can have many LLM calls (one per agent step)."""
    run = make_run(session)
    for i in range(5):
        make_llm_call(session, run, cost_usd=0.001)
    session.refresh(run)

    assert len(run.llm_calls) == 5
    assert run.total_cost_usd == pytest.approx(0.005)


# -- Reminder tests -----------------------------------------------------------


def test_reminder_defaults(session):
    """A new reminder starts as PENDING."""
    family = make_family(session)
    run = make_run(session)
    reminder = Reminder(
        agent_run_id=run.id,
        family_id=family.id,
        channel=Channel.EMAIL,
        body="Hi Bob, your January payment of $150 is overdue.",
    )
    session.add(reminder)
    session.flush()

    assert reminder.id is not None
    assert reminder.status == ReminderStatus.PENDING
    assert reminder.sent_at is None
    assert reminder.get_charge_ids() == []


def test_reminder_charge_ids_roundtrip(session):
    """set_charge_ids / get_charge_ids serialise and deserialise correctly."""
    family = make_family(session)
    run = make_run(session)
    reminder = Reminder(
        agent_run_id=run.id,
        family_id=family.id,
        channel=Channel.EMAIL,
        body="Payment due.",
    )
    reminder.set_charge_ids([10, 11, 12])
    session.add(reminder)
    session.flush()
    session.refresh(reminder)

    assert reminder.get_charge_ids() == [10, 11, 12]


def test_reminder_with_guardian(session):
    """A reminder can be addressed to a specific guardian."""
    family = make_family(session)
    guardian = make_guardian(session, family)
    run = make_run(session)

    reminder = Reminder(
        agent_run_id=run.id,
        family_id=family.id,
        guardian_id=guardian.id,
        channel=Channel.EMAIL,
        body="Hi Bob, just a reminder.",
    )
    session.add(reminder)
    session.flush()

    assert reminder.guardian_id == guardian.id


def test_reminder_channel_sms(session):
    """Reminders can use SMS channel."""
    family = make_family(session)
    run = make_run(session)
    reminder = Reminder(
        agent_run_id=run.id,
        family_id=family.id,
        channel=Channel.SMS,
        body="Payment reminder (SMS).",
    )
    session.add(reminder)
    session.flush()

    assert reminder.channel == Channel.SMS


# -- Cascade tests ------------------------------------------------------------


def test_cascade_delete_run_removes_llm_calls(session):
    """Deleting an AgentRun deletes all its LlmCall rows."""
    run = make_run(session)
    call = make_llm_call(session, run)
    call_id = call.id

    session.delete(run)
    session.flush()

    assert session.get(LlmCall, call_id) is None


def test_cascade_delete_run_removes_reminders(session):
    """Deleting an AgentRun deletes all its Reminder rows."""
    family = make_family(session)
    run = make_run(session)
    reminder = Reminder(
        agent_run_id=run.id,
        family_id=family.id,
        channel=Channel.EMAIL,
        body="Test reminder.",
    )
    session.add(reminder)
    session.flush()
    reminder_id = reminder.id

    session.delete(run)
    session.flush()

    assert session.get(Reminder, reminder_id) is None


def test_family_reminders_relationship(session):
    """family.reminders returns all reminders for that family."""
    family = make_family(session)
    run = make_run(session)
    for i in range(3):
        r = Reminder(
            agent_run_id=run.id,
            family_id=family.id,
            channel=Channel.EMAIL,
            body=f"Reminder {i}",
        )
        session.add(r)
    session.flush()
    session.refresh(family)

    assert len(family.reminders) == 3
