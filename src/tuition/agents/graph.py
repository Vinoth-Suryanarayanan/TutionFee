"""
src/tuition/agents/graph.py
-----------------------------
Assembles the LangGraph StateGraph from the individual nodes.

Graph shape
-----------

  START
    │
    ▼
  ingestion_node          ← imports PayPal + bank CSVs
    │
    ▼
  reconciliation_node     ← runs billing + reconcile + LLM for hard cases
    │
    ▼
  human_review_node       ← INTERRUPT: pauses and shows pending charges
    │                        operator inspects and resumes with approval
    ▼
  reminder_node           ← drafts reminder messages for unpaid families
    │
    ▼
  END

The interrupt after reconciliation_node means the graph saves its state
(via LangGraph's checkpointer) and waits.  The operator resumes it by
calling graph.invoke() again with the same thread_id and human_approved=True.

Human-review node
-----------------
This is a "passthrough" node — it doesn't call the LLM. Its only job is
to be the point where LangGraph pauses.  The actual approval comes from the
operator resuming the graph (via CLI or API) after reviewing the output.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from tuition.agents.ingestion import ingestion_node
from tuition.agents.reconciliation import reconciliation_node
from tuition.agents.reminder import reminder_node
from tuition.agents.state import AgentState


# ── Human-review passthrough node ─────────────────────────────────────────────

def human_review_node(state: AgentState) -> AgentState:
    """Passthrough node — LangGraph interrupts HERE before this runs.

    When the graph is configured with interrupt_before=["human_review"],
    execution pauses before entering this function.  The operator sees the
    current state (pending_charges), decides whether to approve, then
    resumes the graph with human_approved=True in the state update.

    This node itself just passes state through unchanged.
    """
    return state


# ── Graph builder ──────────────────────────────────────────────────────────────

def build_graph(*, interrupt_before_reminder: bool = True) -> StateGraph:
    """Build and compile the tuition agent graph.

    Parameters
    ----------
    interrupt_before_reminder : bool
        If True (default), the graph pauses before human_review_node so
        a human can inspect pending charges before reminders are sent.
        Set to False in tests to run end-to-end without interruption.

    Returns
    -------
    Compiled StateGraph ready to invoke.
    """
    builder = StateGraph(AgentState)

    # Register nodes
    builder.add_node("ingestion", ingestion_node)
    builder.add_node("reconciliation", reconciliation_node)
    builder.add_node("human_review", human_review_node)
    builder.add_node("reminder", reminder_node)

    # Wire edges
    builder.add_edge(START, "ingestion")
    builder.add_edge("ingestion", "reconciliation")
    builder.add_edge("reconciliation", "human_review")
    builder.add_edge("human_review", "reminder")
    builder.add_edge("reminder", END)

    # Checkpointer keeps state between the interrupt and resume
    checkpointer = MemorySaver()

    interrupt_nodes = ["human_review"] if interrupt_before_reminder else []

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_nodes,
    )


# ── Default compiled graph ─────────────────────────────────────────────────────
# Import this in run.py and in tests (tests use interrupt_before_reminder=False)

graph = build_graph(interrupt_before_reminder=True)
