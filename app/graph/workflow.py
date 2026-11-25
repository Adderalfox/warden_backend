"""
Compiles the Warden LangGraph StateGraph. Uses LangGraph's native
interrupt/checkpoint mechanism (via a MemorySaver checkpointer + the
`interrupt()` call inside HumanApprovalNode) so execution genuinely pauses at
AWAITING_APPROVAL and is resumed later by a second `ainvoke` call with a
`Command(resume=...)` -- no manual sleep/poll loop.
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.graph.nodes import (
    context_gather_node,
    diagnostic_node,
    human_approval_node,
    ingest_node,
    patch_generation_node,
    remediation_node,
    validation_node,
)
from app.graph.state import IncidentState


def _route_after_ingest(state: IncidentState) -> str:
    return "diagnostic" if state["status"] != "FAILED" else END


def _route_after_diagnostic(state: IncidentState) -> str:
    return "context_gather" if state["status"] != "FAILED" else END


def _route_after_context(state: IncidentState) -> str:
    return "patch_generation" if state["status"] != "FAILED" else END


def _route_after_patch(state: IncidentState) -> str:
    return "validation" if state["status"] == "PATCH_GENERATED" else END


def _route_after_validation(state: IncidentState) -> str:
    return "human_approval" if state["status"] == "AWAITING_APPROVAL" else END


def _route_after_approval(state: IncidentState) -> str:
    return "remediation"


def build_graph():
    graph = StateGraph(IncidentState)

    graph.add_node("ingest", ingest_node)
    graph.add_node("diagnostic", diagnostic_node)
    graph.add_node("context_gather", context_gather_node)
    graph.add_node("patch_generation", patch_generation_node)
    graph.add_node("validation", validation_node)
    graph.add_node("human_approval", human_approval_node)
    graph.add_node("remediation", remediation_node)

    graph.set_entry_point("ingest")
    graph.add_conditional_edges("ingest", _route_after_ingest)
    graph.add_conditional_edges("diagnostic", _route_after_diagnostic)
    graph.add_conditional_edges("context_gather", _route_after_context)
    graph.add_conditional_edges("patch_generation", _route_after_patch)
    graph.add_conditional_edges("validation", _route_after_validation)
    graph.add_conditional_edges("human_approval", _route_after_approval)
    graph.add_edge("remediation", END)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


# Process-wide compiled graph + checkpointer-backed state.
warden_graph = build_graph()
