"""
src/tuition/agents/ingestion.py
--------------------------------
Ingestion node: imports PayPal and bank CSVs into the transactions table.

What this node does
-------------------
1. Reads paypal_csv_path and bank_csv_path from state.
2. Calls import_paypal_csv_tool and import_bank_csv_tool (MCP tool functions).
3. Writes the combined result to state["ingestion_result"].
4. Appends any errors to state["errors"].

No LLM is called here — this is pure data loading.
The MCP tools handle idempotency: re-running with the same CSV is safe.

Why call the tool functions directly instead of over MCP protocol?
-------------------------------------------------------------------
Within the same Python process, we import the tool functions directly.
The MCP server wraps them for external callers (Claude Desktop, curl).
Inside agents/, we skip the network hop and call the functions as regular
Python — same logic, faster, no serialisation overhead.
"""

from __future__ import annotations

from tuition.agents.state import AgentState
from tuition.tools.mcp_server import import_bank_csv_tool, import_paypal_csv_tool


def ingestion_node(state: AgentState) -> AgentState:
    """LangGraph node: import PayPal and bank CSVs.

    Reads
    -----
    state["paypal_csv_path"]  : str
    state["bank_csv_path"]    : str

    Writes
    ------
    state["ingestion_result"] : dict
    state["errors"]           : list (appended to)
    """
    errors: list[str] = list(state.get("errors") or [])

    paypal_path = state.get("paypal_csv_path", "")
    bank_path = state.get("bank_csv_path", "")

    # ── Import PayPal CSV ──────────────────────────────────────────────────
    if paypal_path:
        paypal_result = import_paypal_csv_tool(paypal_path)
        errors.extend(paypal_result.get("errors", []))
    else:
        paypal_result = {"inserted": 0, "skipped": 0, "errors": ["No PayPal CSV path provided"]}
        errors.append("No PayPal CSV path provided")

    # ── Import bank CSV ────────────────────────────────────────────────────
    if bank_path:
        bank_result = import_bank_csv_tool(bank_path)
        errors.extend(bank_result.get("errors", []))
    else:
        bank_result = {"inserted": 0, "skipped": 0, "errors": ["No bank CSV path provided"]}
        errors.append("No bank CSV path provided")

    return {
        **state,
        "ingestion_result": {
            "paypal": paypal_result,
            "bank": bank_result,
        },
        "errors": errors,
    }
